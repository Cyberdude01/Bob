"""
Polymarket Live Trade Smoke Test
=================================
Diagnoses API connectivity and order submission in live mode.

Stages
------
  Stage 1  Load & validate credentials from /etc/polymarket.env (or environment)
  Stage 2  GET /balance-allowance  — confirm L2 auth headers work
  Stage 3  Fetch a live BTC or ETH 15-min market via Gamma API
  Stage 4  GET last-trade-price  — confirm CLOB data access
  Stage 5  Build a signed EIP-712 order (dry-run by default)
  Stage 6  POST /order  — ONLY runs with --execute flag (REAL MONEY)

Usage (both forms work on the production server)
-----
  # As a module (from /root):
  cd /root && python -m polymarket.smoke_test

  # As a standalone script (from anywhere):
  python /root/polymarket/smoke_test.py

  # Re-derive fresh API credentials from private key (fixes 401 errors):
  python /root/polymarket/smoke_test.py --rederive

  # Real $1 order (REAL MONEY):
  python /root/polymarket/smoke_test.py --execute

  # Custom env file:
  python /root/polymarket/smoke_test.py --env /path/to/my.env

The script prints a pass/fail summary for each stage so you can pinpoint
exactly where the live trading pipeline breaks.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# ─── Path bootstrap (supports both module and standalone invocation) ───────────
# When run as `python /root/polymarket/smoke_test.py`, the package root (/root)
# is not on sys.path automatically.  Add it so absolute imports work.
_THIS_DIR = Path(__file__).resolve().parent          # /root/polymarket
_PKG_ROOT  = _THIS_DIR.parent                        # /root
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _load_env(path: str) -> None:
    """Load KEY=VALUE lines from a file into os.environ (skips comments)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _l2_headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    api_key    = os.environ.get("POLY_API_KEY", "")
    api_secret = os.environ.get("POLY_API_SECRET", "")
    passphrase = os.environ.get("POLY_API_PASSPHRASE", "")
    address    = os.environ.get("POLY_ADDRESS", "")
    if not (api_key and api_secret):
        return {}
    ts      = str(int(time.time()))
    message = ts + method.upper() + path + (body or "")
    try:
        secret_bytes = base64.b64decode(api_secret)
    except Exception:
        secret_bytes = api_secret.encode()
    sig = base64.b64encode(
        hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
    ).decode()
    return {
        "POLY-ADDRESS":    address,
        "POLY-SIGNATURE":  sig,
        "POLY-TIMESTAMP":  ts,
        "POLY-API-KEY":    api_key,
        "POLY-PASSPHRASE": passphrase,
    }


_PASS  = "  \033[92m✔ PASS\033[0m"
_FAIL  = "  \033[91m✖ FAIL\033[0m"
_WARN  = "  \033[93m⚠ WARN\033[0m"
_INFO  = "  \033[94mℹ INFO\033[0m"
_SKIP  = "  \033[90m- SKIP\033[0m"

CLOB_API  = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


# ─── Main test runner ─────────────────────────────────────────────────────────

async def run(execute: bool = False) -> None:
    import aiohttp

    print("\n" + "="*60)
    print("  Polymarket Live Trade Smoke Test")
    print("="*60 + "\n")

    results: Dict[str, str] = {}

    # ── Stage 1: Credentials ──────────────────────────────────────────────────
    print("Stage 1 — Credentials")
    required = ["POLY_PRIVATE_KEY", "POLY_ADDRESS", "POLY_API_KEY",
                "POLY_API_SECRET", "POLY_API_PASSPHRASE"]
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"{_FAIL}  Missing: {', '.join(missing)}")
        print(f"{_INFO}  Set these in /etc/polymarket.env or environment")
        results["credentials"] = "FAIL"
        _summary(results)
        return
    print(f"{_PASS}  All 5 credentials present")
    print(f"{_INFO}  POLY_ADDRESS = {os.environ['POLY_ADDRESS']}")
    results["credentials"] = "PASS"

    async with aiohttp.ClientSession() as session:

        # ── Stage 2: Balance / Auth ───────────────────────────────────────────
        print("\nStage 2 — L2 Auth (GET /balance-allowance)")
        # First verify basic connectivity with a public endpoint
        try:
            async with session.get(
                f"{CLOB_API}/time",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    print(f"{_INFO}  CLOB connectivity OK (GET /time → 200)")
                else:
                    print(f"{_WARN}  CLOB /time returned {r.status} — network issue?")
        except Exception as exc:
            print(f"{_WARN}  CLOB /time failed: {exc}")

        # Correct endpoint is /balance-allowance (not /balance)
        _BALANCE_PATH = "/balance-allowance"
        _SIG_TYPE = int(os.environ.get("POLY_SIGNATURE_TYPE", "1"))
        try:
            l2 = _l2_headers("GET", _BALANCE_PATH)
            headers = {"Content-Type": "application/json", **l2}
            masked_key = (l2.get("POLY-API-KEY") or "")[:8] + "…"
            print(f"{_INFO}  Headers: POLY-ADDRESS={l2.get('POLY-ADDRESS','?')[:10]}…  POLY-API-KEY={masked_key}")
            print(f"{_INFO}  Using signature_type={_SIG_TYPE} (POLY_SIGNATURE_TYPE)")
            async with session.get(
                f"{CLOB_API}{_BALANCE_PATH}?asset_type=0&signature_type={_SIG_TYPE}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                body = await r.text()
                if r.status == 200:
                    data      = json.loads(body)
                    balance   = float(data.get("balance") or data.get("USDC") or
                                      data.get("asset_balance") or 0)
                    allowance = float(data.get("allowance") or 0)
                    print(f"{_PASS}  Status 200 — USDC balance: ${balance:.4f}  allowance: ${allowance:.4f}")
                    print(f"{_INFO}  Full response: {json.dumps(data)}")
                    results["auth_balance"] = "PASS"
                elif r.status in (401, 403):
                    print(f"{_FAIL}  Status {r.status}: Auth rejected — credentials invalid or expired")
                    print(f"{_INFO}  Ensure POLY_SIGNATURE_TYPE=1 is set in /etc/polymarket.env")
                    print(f"{_INFO}  Or re-derive API keys: python -m polymarket.smoke_test --rederive")
                    print(f"{_INFO}  Response: {body[:300]}")
                    results["auth_balance"] = "FAIL"
                else:
                    print(f"{_FAIL}  Status {r.status}: {body[:300]}")
                    print(f"{_INFO}  Check POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE")
                    results["auth_balance"] = "FAIL"
        except Exception as exc:
            print(f"{_FAIL}  Exception: {exc}")
            results["auth_balance"] = "FAIL"

        # ── Stage 3: Fetch live crypto 15-min market (BTC or ETH) ────────────
        print("\nStage 3 — Fetch live crypto 15-min market (Gamma API)")
        market: Optional[Dict[str, Any]] = None
        token_id_up: Optional[str]       = None
        condition_id: Optional[str]      = None
        # Try BTC first, then ETH — markets alternate/run in parallel
        _candidates = [
            ("btc-updown-15m", "BTC"),
            ("eth-updown-15m", "ETH"),
        ]
        for _slug_prefix, _symbol in _candidates:
            try:
                async with session.get(
                    f"{GAMMA_API}/markets?tag=crypto&active=true&slug={_slug_prefix}",
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    data = await r.json()
                markets = data if isinstance(data, list) else data.get("markets", [])
                if markets:
                    market       = markets[0]
                    condition_id = market.get("conditionId") or market.get("condition_id")
                    tokens       = market.get("tokens", [])
                    for tok in tokens:
                        if tok.get("outcome", "").upper() == "UP":
                            token_id_up = tok.get("token_id") or tok.get("tokenId")
                            break
                    print(f"{_PASS}  Found {_symbol} market: {market.get('slug', '?')}")
                    print(f"{_INFO}  condition_id  = {condition_id}")
                    print(f"{_INFO}  token_id_up   = {token_id_up}")
                    results["market_fetch"] = "PASS"
                    break
            except Exception as exc:
                print(f"{_WARN}  {_symbol} fetch error: {exc}")
        else:
            if market is None:
                print(f"{_WARN}  No active BTC or ETH 15-min market found (might be between windows)")
                results["market_fetch"] = "WARN"

        # ── Stage 4: CLOB last-trade-price ────────────────────────────────────
        print("\nStage 4 — CLOB last-trade-price")
        up_price: float = 0.48
        if token_id_up:
            try:
                async with session.get(
                    f"{CLOB_API}/last-trade-price?token_id={token_id_up}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    data     = await r.json()
                    up_price = float(data.get("price", 0.48))
                print(f"{_PASS}  UP last-trade-price: {up_price:.4f}")
                print(f"{_INFO}  DOWN implied:        {round(1-up_price, 4):.4f}")
                results["clob_price"] = "PASS"
            except Exception as exc:
                print(f"{_FAIL}  Exception: {exc}")
                results["clob_price"] = "FAIL"
        else:
            print(f"{_SKIP}  No token_id_up (Stage 3 failed)")
            results["clob_price"] = "SKIP"

        # ── Stage 5: Build signed order (dry-run) ─────────────────────────────
        print("\nStage 5 — Build signed EIP-712 order (dry-run, no submission)")
        order_body: Optional[dict] = None
        if token_id_up:
            try:
                from .trader import OrderBuilder
                private_key = os.environ.get("POLY_PRIVATE_KEY", "")
                address     = os.environ.get("POLY_ADDRESS", "")
                builder     = OrderBuilder(private_key, address)

                # $1 minimum order, BUY UP at current price
                order_body, sig = builder.build_limit_order(
                    token_id = token_id_up,
                    side     = __import__("polymarket.models", fromlist=["Side"]).Side.BUY,
                    size     = 1.0,
                    price    = round(up_price, 4),
                )
                sig_preview = sig[:20] + "…" if sig else "INVALID"
                print(f"{_PASS}  Order built — EIP-712 sig: {sig_preview}")
                print(f"{_INFO}  token_id:     {token_id_up}")
                print(f"{_INFO}  side:         BUY UP  @ {up_price:.4f}")
                print(f"{_INFO}  size:         $1.00 USDC")
                print(json.dumps(order_body["order"], indent=4))
                results["order_build"] = "PASS"
            except Exception as exc:
                print(f"{_FAIL}  Exception building order: {exc}")
                results["order_build"] = "FAIL"
        else:
            print(f"{_SKIP}  No token_id_up (Stage 3 failed)")
            results["order_build"] = "SKIP"

        # ── Stage 6: POST /order (REAL MONEY — only with --execute) ──────────
        print("\nStage 6 — POST /order to exchange")
        if not execute:
            print(f"{_SKIP}  Dry-run mode — skipping real order submission")
            print(f"{_INFO}  Re-run with --execute to submit a real $1 order")
            results["order_post"] = "SKIP"
        elif order_body is None:
            print(f"{_SKIP}  Stage 5 failed — cannot submit order")
            results["order_post"] = "SKIP"
        else:
            print(f"\033[93m  ⚠ REAL MONEY: submitting $1 BUY UP order to Polymarket…\033[0m")
            try:
                body_s  = json.dumps(order_body)
                headers = {
                    "Content-Type": "application/json",
                    **_l2_headers("POST", "/order", body_s),
                }
                async with session.post(
                    f"{CLOB_API}/order",
                    data    = body_s,
                    headers = headers,
                    timeout = aiohttp.ClientTimeout(total=15),
                ) as r:
                    resp_body = await r.text()
                    try:
                        resp = json.loads(resp_body)
                    except Exception:
                        resp = {"raw": resp_body}

                    if r.status == 200:
                        order_id = resp.get("orderId") or resp.get("order_id") or "?"
                        print(f"{_PASS}  Order ACCEPTED — orderId: {order_id}")
                        print(f"{_INFO}  Full response: {json.dumps(resp, indent=4)}")
                        results["order_post"] = "PASS"
                    else:
                        print(f"{_FAIL}  Status {r.status} — exchange rejected order")
                        print(f"{_INFO}  Response: {resp_body[:600]}")
                        print()
                        print("  Common causes:")
                        print("   • Invalid EIP-712 signature (wrong private key / domain)")
                        print("   • Token ID mismatch or expired market")
                        print("   • Insufficient USDC balance")
                        print("   • API key not whitelisted for trading")
                        results["order_post"] = "FAIL"
            except Exception as exc:
                print(f"{_FAIL}  Exception: {exc}")
                results["order_post"] = "FAIL"

    _summary(results)


def _summary(results: Dict[str, str]) -> None:
    print("\n" + "="*60)
    print("  Summary")
    print("="*60)
    icons = {"PASS": "\033[92m✔\033[0m", "FAIL": "\033[91m✖\033[0m",
             "WARN": "\033[93m⚠\033[0m", "SKIP": "\033[90m-\033[0m"}
    labels = {
        "credentials":  "Stage 1  Credentials",
        "auth_balance": "Stage 2  L2 Auth / Balance-Allowance",
        "market_fetch": "Stage 3  Market Fetch (Gamma)",
        "clob_price":   "Stage 4  CLOB Price",
        "order_build":  "Stage 5  Order Build (dry-run)",
        "order_post":   "Stage 6  Order Post (live)",
    }
    any_fail = False
    for key, label in labels.items():
        status = results.get(key, "SKIP")
        icon   = icons.get(status, "-")
        print(f"  {icon}  {label:35s} {status}")
        if status == "FAIL":
            any_fail = True

    print()
    if any_fail:
        print("  \033[91mOne or more stages FAILED — live trading will not work correctly.\033[0m")
        print("  Fix the issues above and re-run the smoke test.\n")
    else:
        print("  \033[92mAll tested stages passed.\033[0m\n")


def rederive_credentials() -> None:
    """
    Re-derive Polymarket API credentials from the private key using
    py_clob_client (handles email-wallet nonce correctly).
    Prints new POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE so the
    user can paste them into /etc/polymarket.env.
    """
    from py_clob_client.client import ClobClient

    print("\n" + "="*60)
    print("  Polymarket Credential Re-Derivation")
    print("="*60 + "\n")

    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    address     = os.environ.get("POLY_ADDRESS", "")
    sig_type    = int(os.environ.get("POLY_SIGNATURE_TYPE", "0"))
    if not private_key or not address:
        print(f"{_FAIL}  POLY_PRIVATE_KEY and POLY_ADDRESS must be set in /etc/polymarket.env")
        return

    try:
        print(f"{_INFO}  Deriving credentials (signature_type={sig_type})…")
        client = ClobClient(
            CLOB_API,
            key=private_key,
            chain_id=137,
            signature_type=sig_type,
            funder=address,
        )
        creds = client.derive_api_key()
        api_key    = creds.api_key
        api_secret = creds.api_secret
        passphrase = creds.api_passphrase
        print(f"{_PASS}  New credentials generated!\n")
        print("  ┌─ Copy these into /etc/polymarket.env ────────────────")
        print(f"  │  POLY_API_KEY={api_key}")
        print(f"  │  POLY_API_SECRET={api_secret}")
        print(f"  │  POLY_API_PASSPHRASE={passphrase}")
        print("  └──────────────────────────────────────────────────────\n")
        print("  Then restart the service:")
        print("    sudo systemctl restart polymarket")
        print("  And re-run the smoke test:")
        print("    cd /root && python -m polymarket.smoke_test")
    except Exception as exc:
        print(f"{_FAIL}  derive_api_key failed: {exc}")


if __name__ == "__main__":
    execute  = "--execute"  in sys.argv
    rederive = "--rederive" in sys.argv
    env_arg  = next((sys.argv[i+1] for i, a in enumerate(sys.argv)
                     if a == "--env" and i+1 < len(sys.argv)), None)

    # Load env file (default: /etc/polymarket.env)
    _load_env(env_arg or "/etc/polymarket.env")

    if rederive:
        rederive_credentials()
        sys.exit(0)

    if execute:
        print("\033[93mWARNING: --execute flag set. A real $1 order will be submitted.\033[0m")
        print("Press Ctrl-C within 5 seconds to abort…")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(0)

    asyncio.run(run(execute=execute))
