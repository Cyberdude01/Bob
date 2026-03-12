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
USDC            = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (PoS)
CTF_EXCHANGE    = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # buy/sell standard markets
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"  # neg-risk markets
CTF_TOKEN       = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # ERC-1155 conditional tokens

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

    w3   = Web3(Web3.HTTPProvider("https://polygon.llamarpc.com"))
    acct = Account.from_key(private_key)
    print(f"  {INFO}  wallet:  {acct.address}")
    print(f"  {INFO}  chain:   {w3.eth.chain_id}  (block {w3.eth.block_number})")

    usdc = w3.eth.contract(address=w3.to_checksum_address(USDC), abi=USDC_ABI)
    ctf  = w3.eth.contract(address=w3.to_checksum_address(CTF_TOKEN), abi=CTF_ABI)

    # ── 1. USDC → CTFExchange ─────────────────────────────────────────────
    cur = usdc.functions.allowance(
        w3.to_checksum_address(acct.address),
        w3.to_checksum_address(CTF_EXCHANGE),
    ).call()
    print(f"\n  {INFO}  USDC allowance (CTFExchange):    ${cur / 1e6:,.2f}")
    if cur < ALLOWANCE_USDC:
        _send(w3, acct, usdc.functions.approve(
            w3.to_checksum_address(CTF_EXCHANGE), ALLOWANCE_USDC
        ), "USDC.approve(CTFExchange, $60,000)")
    else:
        print(f"  {GREEN}  Already ≥ $60,000 — skipping")

    # ── 2. USDC → NegRiskAdapter ──────────────────────────────────────────
    cur2 = usdc.functions.allowance(
        w3.to_checksum_address(acct.address),
        w3.to_checksum_address(NEG_RISK_ADAPTER),
    ).call()
    print(f"\n  {INFO}  USDC allowance (NegRiskAdapter): ${cur2 / 1e6:,.2f}")
    if cur2 < ALLOWANCE_USDC:
        _send(w3, acct, usdc.functions.approve(
            w3.to_checksum_address(NEG_RISK_ADAPTER), ALLOWANCE_USDC
        ), "USDC.approve(NegRiskAdapter, $60,000)")
    else:
        print(f"  {GREEN}  Already ≥ $60,000 — skipping")

    # ── 3. CTF tokens → NegRiskAdapter (for selling) ──────────────────────
    approved = ctf.functions.isApprovedForAll(
        w3.to_checksum_address(acct.address),
        w3.to_checksum_address(NEG_RISK_ADAPTER),
    ).call()
    print(f"\n  {INFO}  CTF.isApprovedForAll(NegRiskAdapter): {approved}")
    if not approved:
        _send(w3, acct, ctf.functions.setApprovalForAll(
            w3.to_checksum_address(NEG_RISK_ADAPTER), True
        ), "CTF.setApprovalForAll(NegRiskAdapter)")
    else:
        print(f"  {GREEN}  Already approved — skipping")

    print(f"\n  {GREEN}  All allowances set. Ready to trade.\n")


if __name__ == "__main__":
    main()
