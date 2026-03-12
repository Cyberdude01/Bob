"""
Polymarket One-Time Allowance Setup
=====================================
Sets on-chain USDC and conditional-token approvals required before trading.

  Buying  → USDC.e must be approved for CTFExchange + NegRiskAdapter
  Selling → CTF tokens must be approved (setApprovalForAll) for the same

Run once (or whenever allowance is exhausted):
  python -m polymarket.setup_allowance

Requires: POLY_PRIVATE_KEY and POLY_ADDRESS in .env
"""
import os
import sys
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account

load_dotenv("/etc/polymarket.env")
load_dotenv()  # also load local .env if present (overrides)

# ── Contract addresses (Polygon mainnet, chain 137) ────────────────────────
# Source: https://docs.polymarket.com/contract-addresses
USDC                 = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (PoS)
CTF_TOKEN            = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # ERC-1155 conditional tokens
CTF_EXCHANGE         = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # standard market exchange
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"  # neg-risk market exchange
NEG_RISK_ADAPTER     = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"  # neg-risk adapter

# $60,000 USDC (6 decimals)
ALLOWANCE_USDC  = 60_000 * 10**6

# ── ABIs (minimal) ─────────────────────────────────────────────────────────
USDC_ABI = [
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]

CTF_ABI = [
    {"inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
     "name": "setApprovalForAll", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}],
     "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
]

# ── Helpers ────────────────────────────────────────────────────────────────
GREEN  = "\033[92m✔\033[0m"
RED    = "\033[91m✖\033[0m"
INFO   = "\033[94mi\033[0m"

def _send(w3, acct, fn, label):
    """Build, sign, send a transaction and wait for receipt."""
    tx = fn.build_transaction({
        "from":                 acct.address,
        "nonce":                w3.eth.get_transaction_count(acct.address),
        "gas":                  120_000,
        "maxFeePerGas":         w3.to_wei("150", "gwei"),
        "maxPriorityFeePerGas": w3.to_wei("30",  "gwei"),
        "chainId":              137,
    })
    signed  = w3.eth.account.sign_transaction(tx, acct.key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  {INFO}  {label}: tx {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
    if receipt.status == 1:
        print(f"  {GREEN}  {label}: confirmed")
    else:
        print(f"  {RED}  {label}: REVERTED")
        sys.exit(1)


def main():
    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    if not private_key:
        print(f"  {RED}  POLY_PRIVATE_KEY not set in .env")
        sys.exit(1)

    _rpc_candidates = [
        os.environ.get("POLYGON_RPC_URL", ""),
        "https://rpc.ankr.com/polygon/b77f5e0dd955373ca4c9e3d668d87d8217aaa907b55aa39d430ccc686b78fe22",
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon.drpc.org",
        "https://1rpc.io/matic",
        "https://polygon.llamarpc.com",
    ]
    w3 = None
    for _rpc in _rpc_candidates:
        if not _rpc:
            continue
        try:
            _w3 = Web3(Web3.HTTPProvider(_rpc, request_kwargs={"timeout": 10}))
            _ = _w3.eth.block_number  # connectivity check
            w3 = _w3
            print(f"  {INFO}  RPC:     {_rpc}")
            break
        except Exception:
            continue
    if w3 is None:
        print(f"  {RED}  All Polygon RPC endpoints failed — check network connectivity")
        sys.exit(1)
    acct = Account.from_key(private_key)
    print(f"  {INFO}  wallet:  {acct.address}")
    print(f"  {INFO}  chain:   {w3.eth.chain_id}  (block {w3.eth.block_number})")

    usdc = w3.eth.contract(address=w3.to_checksum_address(USDC), abi=USDC_ABI)
    ctf  = w3.eth.contract(address=w3.to_checksum_address(CTF_TOKEN), abi=CTF_ABI)

    # Per docs.polymarket.com/contract-addresses, three approvals are required:
    # 1. USDC.e → CTF Contract  (splits USDC.e into outcome tokens)
    # 2. CTF tokens → CTF Exchange  (trade standard markets)
    # 3. CTF tokens → Neg Risk CTF Exchange  (trade neg-risk markets)
    # We also approve USDC.e → NegRiskAdapter for neg-risk USDC spending.

    # ── 1. USDC.e → CTF Contract ──────────────────────────────────────────
    cur = usdc.functions.allowance(
        w3.to_checksum_address(acct.address),
        w3.to_checksum_address(CTF_TOKEN),
    ).call()
    print(f"\n  {INFO}  USDC allowance (CTF Contract):       ${cur / 1e6:,.2f}")
    if cur < ALLOWANCE_USDC:
        _send(w3, acct, usdc.functions.approve(
            w3.to_checksum_address(CTF_TOKEN), ALLOWANCE_USDC
        ), "USDC.approve(CTF Contract, $60,000)")
    else:
        print(f"  {GREEN}  Already ≥ $60,000 — skipping")

    # ── 2. USDC.e → NegRiskAdapter ────────────────────────────────────────
    cur2 = usdc.functions.allowance(
        w3.to_checksum_address(acct.address),
        w3.to_checksum_address(NEG_RISK_ADAPTER),
    ).call()
    print(f"\n  {INFO}  USDC allowance (NegRiskAdapter):     ${cur2 / 1e6:,.2f}")
    if cur2 < ALLOWANCE_USDC:
        _send(w3, acct, usdc.functions.approve(
            w3.to_checksum_address(NEG_RISK_ADAPTER), ALLOWANCE_USDC
        ), "USDC.approve(NegRiskAdapter, $60,000)")
    else:
        print(f"  {GREEN}  Already ≥ $60,000 — skipping")

    # ── 3. CTF tokens → CTF Exchange ──────────────────────────────────────
    approved = ctf.functions.isApprovedForAll(
        w3.to_checksum_address(acct.address),
        w3.to_checksum_address(CTF_EXCHANGE),
    ).call()
    print(f"\n  {INFO}  CTF.isApprovedForAll(CTFExchange):      {approved}")
    if not approved:
        _send(w3, acct, ctf.functions.setApprovalForAll(
            w3.to_checksum_address(CTF_EXCHANGE), True
        ), "CTF.setApprovalForAll(CTFExchange)")
    else:
        print(f"  {GREEN}  Already approved — skipping")

    # ── 4. CTF tokens → Neg Risk CTF Exchange ─────────────────────────────
    approved2 = ctf.functions.isApprovedForAll(
        w3.to_checksum_address(acct.address),
        w3.to_checksum_address(NEG_RISK_CTF_EXCHANGE),
    ).call()
    print(f"\n  {INFO}  CTF.isApprovedForAll(NegRiskCTFExchange): {approved2}")
    if not approved2:
        _send(w3, acct, ctf.functions.setApprovalForAll(
            w3.to_checksum_address(NEG_RISK_CTF_EXCHANGE), True
        ), "CTF.setApprovalForAll(NegRiskCTFExchange)")
    else:
        print(f"  {GREEN}  Already approved — skipping")

    print(f"\n  {GREEN}  All allowances set. Ready to trade.\n")


if __name__ == "__main__":
    main()
