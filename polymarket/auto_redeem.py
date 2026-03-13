"""
Polymarket Auto Redeem
======================
Polls for redeemable positions every 10 minutes and calls redeemPositions()
on-chain via the ProxyWalletFactory.

For sig_type=1 (POLY_PROXY) accounts, CTF tokens are held by the proxy wallet
contract.  redeemPositions() must be called FROM the proxy wallet, which is done
by routing the call through ProxyWalletFactory.proxy().

Usage:
  python -m polymarket.auto_redeem            # run continuously, every 10 min
  python -m polymarket.auto_redeem --once     # run one check then exit
  python -m polymarket.auto_redeem --dry-run  # show what would be redeemed, no tx

Requires: POLY_PRIVATE_KEY, POLY_ADDRESS (proxy wallet) in /etc/polymarket.env
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

# ── Path bootstrap ───────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).resolve().parent
_PKG_ROOT  = _THIS_DIR.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# ── Configuration ────────────────────────────────────────────────────────────
DATA_API    = "https://data-api.polymarket.com"
GAMMA_API   = "https://gamma-api.polymarket.com"

USDC        = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_TOKEN   = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

PROXY_FACTORY     = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
POLL_INTERVAL_SEC = 600   # 10 minutes

# ── ABIs (minimal) ───────────────────────────────────────────────────────────
FACTORY_ABI = [
    {
        "name": "proxy",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "calls",
                "type": "tuple[]",
                "components": [
                    {"name": "typeCode", "type": "uint8"},
                    {"name": "to",       "type": "address"},
                    {"name": "value",    "type": "uint256"},
                    {"name": "data",     "type": "bytes"},
                ],
            }
        ],
        "outputs": [{"name": "returnValues", "type": "bytes[]"}],
    }
]

CTF_REDEEM_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken",      "type": "address"},
            {"name": "parentCollectionId",   "type": "bytes32"},
            {"name": "conditionId",          "type": "bytes32"},
            {"name": "indexSets",            "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "account",  "type": "address"},
            {"name": "id",       "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

NEG_RISK_REDEEM_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets",   "type": "uint256[]"},
        ],
        "outputs": [],
    }
]

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN = "\033[92m✔\033[0m"
RED   = "\033[91m✖\033[0m"
INFO  = "\033[94mi\033[0m"
WARN  = "\033[93m⚠\033[0m"


# ── RPC helpers ───────────────────────────────────────────────────────────────
_RPC_CANDIDATES = [
    "",   # filled from env at runtime
    "https://rpc.ankr.com/polygon/b77f5e0dd955373ca4c9e3d668d87d8217aaa907b55aa39d430ccc686b78fe22",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon.llamarpc.com",
]

def _connect_rpc() -> Web3:
    candidates = [os.environ.get("POLYGON_RPC_URL", "")] + _RPC_CANDIDATES[1:]
    for rpc in candidates:
        if not rpc:
            continue
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            _ = w3.eth.block_number
            return w3
        except Exception:
            continue
    raise RuntimeError("All Polygon RPC endpoints failed")


# ── Position helpers ──────────────────────────────────────────────────────────
def fetch_redeemable(proxy_address: str) -> List[Dict[str, Any]]:
    """
    Fetch positions from DATA_API and return those that are redeemable.
    Tries /positions then /activity as fallback.
    """
    endpoints = [
        f"{DATA_API}/positions",
        f"{GAMMA_API}/positions",
    ]
    positions: List[Dict] = []
    for url in endpoints:
        try:
            r = requests.get(url, params={"user": proxy_address, "redeemable": "true"}, timeout=15)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                positions = data
            elif isinstance(data, dict):
                positions = data.get("positions", data.get("data", []))
            if positions:
                break
        except Exception as exc:
            print(f"  {WARN}  {url} → {exc}")

    redeemable = []
    for p in positions:
        # Normalise field names — different endpoints use different keys
        won      = p.get("redeemable") or p.get("won") or p.get("isWinner") or p.get("claimable")
        size_raw = p.get("size", p.get("currentTokens", p.get("balance", 0)))
        try:
            size = float(size_raw)
        except (TypeError, ValueError):
            size = 0.0
        if won and size > 0:
            redeemable.append(p)
    return redeemable


def _to_bytes32(hex_str: str) -> bytes:
    h = hex_str.lstrip("0x")
    return bytes.fromhex(h.zfill(64))


# ── On-chain redemption ───────────────────────────────────────────────────────
def redeem_one(
    w3:      Web3,
    acct,
    factory,
    ctf,
    neg_risk_contract,
    position: Dict[str, Any],
    dry_run:  bool = False,
) -> bool:
    """
    Encode and submit a redeemPositions() call for one position through the
    ProxyWalletFactory.  Returns True if the tx confirmed successfully.
    """
    condition_id  = position.get("conditionId", position.get("condition_id", ""))
    outcome_index = int(position.get("outcomeIndex", position.get("outcome_index", 0)))
    neg_risk      = bool(position.get("negRisk", position.get("neg_risk", False)))
    title         = position.get("title", position.get("question", condition_id[:14] + "…"))
    size          = float(position.get("size", position.get("currentTokens", 0)))

    if not condition_id:
        print(f"  {WARN}  Skipping position with no conditionId: {position}")
        return False

    # indexSet: outcome 0 → 1 (bit 0), outcome 1 → 2 (bit 1)
    index_set = 1 << outcome_index

    print(f"\n  {INFO}  Redeeming: {title}")
    print(f"  {INFO}  conditionId={condition_id}  outcomeIndex={outcome_index}  indexSet={index_set}  negRisk={neg_risk}  size={size:.4f}")

    if neg_risk:
        call_data = neg_risk_contract.encodeABI(
            fn_name="redeemPositions",
            args=[_to_bytes32(condition_id), [index_set]],
        )
        target = w3.to_checksum_address(NEG_RISK_ADAPTER)
    else:
        call_data = ctf.encodeABI(
            fn_name="redeemPositions",
            args=[
                w3.to_checksum_address(USDC),
                b"\x00" * 32,                     # parentCollectionId = bytes32(0)
                _to_bytes32(condition_id),
                [index_set],
            ],
        )
        target = w3.to_checksum_address(CTF_TOKEN)

    if dry_run:
        print(f"  {INFO}  [dry-run] would call factory.proxy([{{typeCode:1, to:{target}, data:{call_data[:20]}…}}])")
        return True

    nonce = w3.eth.get_transaction_count(acct.address)
    tx = factory.functions.proxy([
        {"typeCode": 1, "to": target, "value": 0, "data": bytes.fromhex(call_data[2:])}
    ]).build_transaction({
        "from":                 acct.address,
        "nonce":                nonce,
        "gas":                  250_000,
        "maxFeePerGas":         w3.to_wei("150", "gwei"),
        "maxPriorityFeePerGas": w3.to_wei("30",  "gwei"),
        "chainId":              137,
        "value":                0,
    })
    signed  = w3.eth.account.sign_transaction(tx, acct.key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  {INFO}  tx: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status == 1:
        print(f"  {GREEN}  Redeemed — gas used: {receipt.gasUsed:,}")
        return True
    else:
        print(f"  {RED}  Transaction reverted")
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────
def run_once(proxy_address: str, w3: Web3, acct, factory, ctf, neg_risk_contract, dry_run: bool) -> int:
    """Run one redemption check.  Returns number of positions redeemed."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}]  Checking for redeemable positions (proxy={proxy_address[:10]}…)")

    positions = fetch_redeemable(proxy_address)
    if not positions:
        print(f"  {INFO}  No redeemable positions found.")
        return 0

    print(f"  {INFO}  Found {len(positions)} redeemable position(s).")
    redeemed = 0
    for pos in positions:
        try:
            ok = redeem_one(w3, acct, factory, ctf, neg_risk_contract, pos, dry_run=dry_run)
            if ok:
                redeemed += 1
        except Exception as exc:
            print(f"  {RED}  Error redeeming position: {exc}")
    return redeemed


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket auto-redeem daemon")
    parser.add_argument("--once",    action="store_true", help="Run one check then exit")
    parser.add_argument("--dry-run", action="store_true", help="Show positions without transacting")
    args = parser.parse_args()

    load_dotenv("/etc/polymarket.env")
    load_dotenv()

    private_key   = os.environ.get("POLY_PRIVATE_KEY", "")
    proxy_address = os.environ.get("POLY_ADDRESS", "")
    if not private_key or not proxy_address:
        print(f"  {RED}  POLY_PRIVATE_KEY and POLY_ADDRESS must be set in /etc/polymarket.env")
        sys.exit(1)

    proxy_address = Web3.to_checksum_address(proxy_address)

    if not args.dry_run:
        w3 = _connect_rpc()
        print(f"  {INFO}  RPC connected  chain={w3.eth.chain_id}  block={w3.eth.block_number}")
        acct    = Account.from_key(private_key)
        factory = w3.eth.contract(address=w3.to_checksum_address(PROXY_FACTORY), abi=FACTORY_ABI)
        ctf     = w3.eth.contract(address=w3.to_checksum_address(CTF_TOKEN),     abi=CTF_REDEEM_ABI)
        neg_risk_contract = w3.eth.contract(
            address=w3.to_checksum_address(NEG_RISK_ADAPTER), abi=NEG_RISK_REDEEM_ABI
        )
        pol_bal = w3.eth.get_balance(acct.address)
        print(f"  {INFO}  Signer EOA: {acct.address}  POL: {pol_bal/1e18:.4f}")
        print(f"  {INFO}  Proxy wallet: {proxy_address}")
    else:
        print(f"  {INFO}  [dry-run mode — no transactions will be sent]")
        w3 = acct = factory = ctf = neg_risk_contract = None  # type: ignore

    if args.once or args.dry_run:
        run_once(proxy_address, w3, acct, factory, ctf, neg_risk_contract, dry_run=args.dry_run)
        return

    # Continuous loop
    print(f"  {INFO}  Starting auto-redeem loop (interval={POLL_INTERVAL_SEC}s / 10 min)")
    while True:
        try:
            run_once(proxy_address, w3, acct, factory, ctf, neg_risk_contract, dry_run=False)
        except Exception as exc:
            print(f"  {RED}  Unexpected error in redeem loop: {exc}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
