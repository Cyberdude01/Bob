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
DATA_API  = "https://data-api.polymarket.com"


# ─── Main test runner ─────────────────────────────────────────────────────────

async def run(execute: bool = False) -> None:
    import aiohttp

    print("\n" + "="*60)
    print("  Polymarket Live Trade Smoke Test")
    print("="*60 + "\n")

    results: Dict[str, str] = {}

    # ── Stage 1: Credentials ──────────────────────────────────────────────────
    print("Stage 1 — Credentials")
    required = ["POLY_PRIVATE_KEY", "POLY_ADDRESS"]
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"{_FAIL}  Missing: {', '.join(missing)}")
        print(f"{_INFO}  Set these in /etc/polymarket.env or environment")
        results["credentials"] = "FAIL"
        _summary(results)
        return
    print(f"{_PASS}  POLY_PRIVATE_KEY and POLY_ADDRESS present")
    print(f"{_INFO}  POLY_ADDRESS = {os.environ['POLY_ADDRESS']}")
    # Report optional stored creds (not required — derived in Stage 2)
    stored = [k for k in ("POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE")
              if os.environ.get(k)]
    if stored:
        print(f"{_INFO}  Stored API creds present: {', '.join(stored)}")
    else:
        print(f"{_INFO}  No stored API creds — will derive from private key in Stage 2")
    results["credentials"] = "PASS"

    async with aiohttp.ClientSession() as session:

        # ── Stage 2: Balance / Auth ───────────────────────────────────────────
        print("\nStage 2 — L2 Auth (GET /balance-allowance via py_clob_client)")
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

        # Use py_clob_client — the only reliable auth path for this wallet type
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            _sig_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "1"))
            print(f"{_INFO}  signature_type={_sig_type} (POLY_SIGNATURE_TYPE)")
            _env_address = os.environ["POLY_ADDRESS"]
            client = ClobClient(
                CLOB_API,
                key=os.environ["POLY_PRIVATE_KEY"],
                chain_id=137,
                signature_type=_sig_type,
                funder=_env_address,
            )
            client.set_api_creds(client.derive_api_key())
            creds = client.get_api_keys()
            print(f"{_INFO}  Derived POLY-API-KEY={str(getattr(creds, 'api_key', creds))[:8]}…")

            # Discover the actual signer/funder address used by this client
            # For sig_type=1 (POLY_PROXY), this should be the PROXY wallet address,
            # which may differ from POLY_ADDRESS (EOA) in /etc/polymarket.env
            try:
                _signer_obj = client.signer
                _raw_addr = _signer_obj.address() if callable(getattr(_signer_obj, 'address', None)) else getattr(_signer_obj, 'address', None)
                if _raw_addr:
                    print(f"{_INFO}  CLOB signer address (sig_type={_sig_type}): {_raw_addr}")
                    if str(_raw_addr).lower() != str(_env_address).lower():
                        print(f"{_WARN}  *** PROXY WALLET DETECTED ***")
                        print(f"{_WARN}  POLY_ADDRESS in env : {_env_address}  ← EOA")
                        print(f"{_WARN}  Actual proxy wallet : {_raw_addr}  ← where USDC lives")
                        print(f"{_WARN}  Action: set POLY_ADDRESS={_raw_addr} in /etc/polymarket.env")
            except Exception as _sa_exc:
                print(f"{_INFO}  Could not determine signer address: {_sa_exc}")

            # Also try to fetch profile/me from CLOB to get the canonical address
            try:
                import aiohttp as _aio
                _ts2  = str(int(time.time()))
                _msg2 = _ts2 + "GET" + "/profile"
                _key2 = getattr(client.creds, 'api_key', '') or ''
                _sec2 = getattr(client.creds, 'api_secret', '') or ''
                _pss2 = getattr(client.creds, 'api_passphrase', '') or ''
                if _key2 and _sec2:
                    import base64, hmac, hashlib
                    try:
                        _sb2 = base64.b64decode(_sec2)
                    except Exception:
                        _sb2 = _sec2.encode()
                    _sig2 = base64.b64encode(hmac.new(_sb2, _msg2.encode(), hashlib.sha256).digest()).decode()
                    _hdrs2 = {
                        "POLY-ADDRESS":    _env_address,
                        "POLY-SIGNATURE":  _sig2,
                        "POLY-TIMESTAMP":  _ts2,
                        "POLY-API-KEY":    _key2,
                        "POLY-PASSPHRASE": _pss2,
                    }
                    async with session.get(
                        f"{CLOB_API}/profile",
                        headers=_hdrs2,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as _pr:
                        if _pr.status == 200:
                            _profile = await _pr.json()
                            print(f"{_INFO}  CLOB /profile: {_profile}")
                        else:
                            _pt = await _pr.text()
                            print(f"{_INFO}  CLOB /profile → {_pr.status}: {_pt[:120]}")
            except Exception as _pe:
                print(f"{_INFO}  /profile call skipped: {_pe}")

            bal = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            balance   = int(bal.get("balance",   0)) / 1e6
            allowance = int(bal.get("allowance", 0)) / 1e6
            print(f"{_PASS}  USDC balance: ${balance:.2f}  allowance: ${allowance:.2f}")
            results["auth_balance"] = "PASS"
        except Exception as exc:
            print(f"{_FAIL}  Exception: {exc}")
            print(f"{_INFO}  Ensure POLY_PRIVATE_KEY and POLY_ADDRESS are correct")
            print(f"{_INFO}  Ensure POLY_SIGNATURE_TYPE=1 in /etc/polymarket.env")
            results["auth_balance"] = "FAIL"

        # ── Stage 3: Fetch live crypto 15-min market (BTC or ETH) ────────────
        print("\nStage 3 — Fetch live crypto 15-min market (Gamma API)")
        market: Optional[Dict[str, Any]] = None
        token_id_up: Optional[str]       = None
        condition_id: Optional[str]      = None

        # Markets use timestamped slugs: btc-updown-15m-{unix_15min_boundary}
        # Try current window and adjacent ones in case of boundary timing
        _now     = int(time.time())
        _current = (_now // 900) * 900
        _ts_candidates = [_current, _current + 900, _current - 900]
        _base_slugs = [
            ("btc-updown-15m", "BTC"),
            ("eth-updown-15m", "ETH"),
            ("sol-updown-15m", "SOL"),
            ("xrp-updown-15m", "XRP"),
        ]

        _found = False
        for _base_slug, _symbol in _base_slugs:
            if _found:
                break
            for _ts in _ts_candidates:
                _slug = f"{_base_slug}-{_ts}"
                try:
                    async with session.get(
                        f"{GAMMA_API}/events",
                        params={"slug": _slug, "limit": 1},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        data = await r.json()
                    event = (data[0] if isinstance(data, list) and data
                             else data if isinstance(data, dict) and data.get("id") else None)
                    if not event:
                        continue
                    markets_list = event.get("markets", [])
                    if not markets_list:
                        _eid = str(event.get("id", ""))
                        async with session.get(
                            f"{GAMMA_API}/markets",
                            params={"event_id": _eid, "limit": 20},
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as mr:
                            markets_list = await mr.json()
                        if not isinstance(markets_list, list):
                            markets_list = []
                    if not markets_list:
                        continue
                    market       = markets_list[0]
                    condition_id = market.get("conditionId") or market.get("condition_id")

                    # market-data-collector.js approach: clobTokenIds + outcomes are
                    # separate parallel arrays — NOT a 'tokens' dict field (that's None)
                    raw_ids      = market.get("clobTokenIds") or "[]"
                    raw_outcomes = market.get("outcomes") or "[]"
                    if isinstance(raw_ids, str):
                        raw_ids = json.loads(raw_ids)
                    if isinstance(raw_outcomes, str):
                        raw_outcomes = json.loads(raw_outcomes)
                    print(f"{_INFO}  outcomes: {raw_outcomes}")
                    print(f"{_INFO}  token IDs: {[str(t)[:12]+'…' for t in (raw_ids or [])]}")
                    for tid, outcome in zip(raw_ids, raw_outcomes):
                        if str(outcome).upper() in ("UP", "HIGHER", "YES"):
                            token_id_up = str(tid)
                            break
                    # Also try tokens list format (some markets use this)
                    if token_id_up is None:
                        for tok in (market.get("tokens") or []):
                            if isinstance(tok, dict):
                                out = str(tok.get("outcome", "")).upper()
                                if out in ("UP", "HIGHER", "YES"):
                                    token_id_up = str(tok.get("token_id") or tok.get("tokenId", ""))
                                    break
                    # NegRisk markets (up/down, multi-outcome) use NegRiskExchange
                    _is_neg_risk = bool(market.get("negRisk") or market.get("neg_risk"))

                    print(f"{_PASS}  Found {_symbol} market: {_slug}")
                    print(f"{_INFO}  condition_id  = {condition_id}")
                    print(f"{_INFO}  token_id_up   = {token_id_up}")
                    results["market_fetch"] = "PASS"
                    _found = True
                    break
                except Exception as exc:
                    continue

        if not _found:
            print(f"{_WARN}  No active 15-min crypto market found (might be between windows)")
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
        _clob_client = None
        _auth_client = None
        _signed_order = None
        if token_id_up:
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import OrderArgs, OrderType
                from py_clob_client.order_builder.constants import BUY
                import dataclasses

                # Two-client approach:
                # - sig_type=1 client: derive the registered API key + check balance
                # - sig_type=0 client: build/post orders with direct EOA signing
                # This is needed when POLY_ADDRESS == EOA (no proxy contract deployed):
                # sig_type=1 works for API auth but NOT for order signing (requires proxy).
                # sig_type=0 works for order signing but API key is derived differently.
                _auth_sig_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "1"))
                _auth_client = ClobClient(
                    CLOB_API,
                    key            = os.environ["POLY_PRIVATE_KEY"],
                    chain_id       = 137,
                    signature_type = _auth_sig_type,
                    funder         = os.environ["POLY_ADDRESS"],
                )
                _api_creds = _auth_client.derive_api_key()
                _auth_client.set_api_creds(_api_creds)  # needed for balance + update calls

                # Order-signing client uses sig_type=0 (EOA) for valid order signatures,
                # but injects the STORED registered API creds so Polymarket's pre-check
                # sees the recognised account (with $7 balance) not an unknown fresh key.
                _clob_client = ClobClient(
                    CLOB_API,
                    key            = os.environ["POLY_PRIVATE_KEY"],
                    chain_id       = 137,
                    signature_type = 0,
                    funder         = os.environ["POLY_ADDRESS"],
                )
                # Prefer stored creds (registered via web UI) over freshly derived key
                _stored_key = os.environ.get("POLY_API_KEY", "")
                if _stored_key:
                    try:
                        from py_clob_client.clob_types import ApiCreds
                        _stored_creds = ApiCreds(
                            api_key        = _stored_key,
                            api_secret     = os.environ.get("POLY_API_SECRET", ""),
                            api_passphrase = os.environ.get("POLY_API_PASSPHRASE", ""),
                        )
                        _clob_client.set_api_creds(_stored_creds)
                        _auth_client.set_api_creds(_stored_creds)
                        print(f"{_INFO}  Using stored API creds (registered account)")
                    except Exception:
                        _clob_client.set_api_creds(_api_creds)
                else:
                    _clob_client.set_api_creds(_api_creds)

                _signer_addr = _clob_client.signer.address() if callable(getattr(_clob_client.signer, 'address', None)) else getattr(_clob_client.signer, 'address', _clob_client.signer)
                _auth_signer_raw = getattr(_auth_client.signer, 'address', None)
                _auth_signer_addr = _auth_signer_raw() if callable(_auth_signer_raw) else _auth_signer_raw
                _funder_addr = os.environ["POLY_ADDRESS"]
                print(f"{_INFO}  order signer (sig_type=0): {_signer_addr}")
                print(f"{_INFO}  auth  signer (sig_type={_auth_sig_type}): {_auth_signer_addr}")
                print(f"{_INFO}  POLY_ADDRESS (env funder):  {_funder_addr}")
                if _auth_signer_addr and str(_auth_signer_addr).lower() != str(_funder_addr).lower():
                    print(f"{_WARN}  *** MISMATCH: proxy wallet ({_auth_signer_addr}) != POLY_ADDRESS ({_funder_addr})")
                    print(f"{_WARN}  The $7 USDC is at {_auth_signer_addr}")
                    print(f"{_WARN}  Update POLY_ADDRESS={_auth_signer_addr} in /etc/polymarket.env")

                # Build OrderArgs — try with neg_risk if supported by installed version
                _neg_risk = _is_neg_risk if '_is_neg_risk' in dir() else False
                print(f"{_INFO}  neg_risk market: {_neg_risk}")
                try:
                    order_args = OrderArgs(
                        token_id  = token_id_up,
                        price     = round(up_price, 4),
                        size      = 5.0,
                        side      = BUY,
                        neg_risk  = _neg_risk,
                    )
                except TypeError:
                    order_args = OrderArgs(
                        token_id = token_id_up,
                        price    = round(up_price, 4),
                        size     = 5.0,
                        side     = BUY,
                    )
                _signed_order = _clob_client.create_order(order_args)
                sig_preview   = str(_signed_order.signature)[:20] + "…"
                # Serialize order struct to plain dict for display
                o_dict = dataclasses.asdict(_signed_order.order) if dataclasses.is_dataclass(_signed_order.order) else vars(_signed_order.order)
                print(f"{_PASS}  Order built — EIP-712 sig: {sig_preview}")
                print(f"{_INFO}  side:         BUY UP  @ {up_price:.4f}")
                print(f"{_INFO}  size:         $5.00 USDC (minimum)")
                print(f"{_INFO}  order fields: {list(o_dict.keys())}")
                maker_amt = o_dict.get('makerAmount') or o_dict.get('maker_amount', '?')
                taker_amt = o_dict.get('takerAmount') or o_dict.get('taker_amount', '?')
                print(f"{_INFO}  makerAmount:  {maker_amt}")
                print(f"{_INFO}  takerAmount:  {taker_amt}")
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
        elif _signed_order is None or _clob_client is None:
            print(f"{_SKIP}  Stage 5 failed — cannot submit order")
            results["order_post"] = "SKIP"
        else:
            print(f"\033[93m  ⚠ REAL MONEY: submitting $5 BUY UP order to Polymarket…\033[0m")
            try:
                # Ensure USDC allowance is set — use auth client (sig_type=1) for balance
                # so the proxy-wallet $7 balance is visible
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                _bal_client = _auth_client if _auth_client is not None else _clob_client
                bal = _bal_client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                balance   = int(bal.get("balance",   0)) / 1e6
                allowance = int(bal.get("allowance", 0)) / 1e6
                print(f"{_INFO}  balance: ${balance:.2f}  allowance: ${allowance:.2f}")

                # Always check on-chain state to diagnose CLOB vs on-chain discrepancies
                _USDC              = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                _CTF_TOKEN         = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
                _CTF_EXCHANGE      = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
                _NEG_RISK_ADAPT    = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
                _NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
                _USDC_ABI    = [
                    {"inputs":[{"name":"account","type":"address"}],
                     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],
                     "stateMutability":"view","type":"function"},
                    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
                     "name":"approve","outputs":[{"name":"","type":"bool"}],
                     "stateMutability":"nonpayable","type":"function"},
                    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
                     "name":"allowance","outputs":[{"name":"","type":"uint256"}],
                     "stateMutability":"view","type":"function"},
                ]
                try:
                    from web3 import Web3
                    from eth_account import Account as _Acct
                    _rpc_quick = next((r for r in [
                        os.environ.get("POLYGON_RPC_URL",""),
                        "https://polygon-bor-rpc.publicnode.com",
                        "https://polygon.drpc.org",
                    ] if r), None)
                    _w3d = Web3(Web3.HTTPProvider(_rpc_quick, request_kwargs={"timeout":10}))
                    _acct_d = _Acct.from_key(os.environ["POLY_PRIVATE_KEY"])
                    _poly_addr = os.environ["POLY_ADDRESS"]
                    _usdc_c = _w3d.eth.contract(address=_w3d.to_checksum_address(_USDC), abi=_USDC_ABI)
                    _eoa_bal = _usdc_c.functions.balanceOf(_w3d.to_checksum_address(_acct_d.address)).call()
                    _proxy_bal = _usdc_c.functions.balanceOf(_w3d.to_checksum_address(_poly_addr)).call() if _poly_addr != _acct_d.address else _eoa_bal
                    _eoa_allow_ctf = _usdc_c.functions.allowance(_w3d.to_checksum_address(_acct_d.address), _w3d.to_checksum_address(_CTF_TOKEN)).call()
                    _proxy_allow_ctf = _usdc_c.functions.allowance(_w3d.to_checksum_address(_poly_addr), _w3d.to_checksum_address(_CTF_TOKEN)).call() if _poly_addr != _acct_d.address else _eoa_allow_ctf
                    print(f"{_INFO}  On-chain EOA   ({_acct_d.address[:10]}…): balance=${_eoa_bal/1e6:.2f}  allow(CTF)=${_eoa_allow_ctf/1e6:.2f}")
                    if _poly_addr != _acct_d.address:
                        print(f"{_INFO}  On-chain Proxy ({_poly_addr[:10]}…): balance=${_proxy_bal/1e6:.2f}  allow(CTF)=${_proxy_allow_ctf/1e6:.2f}")
                except Exception as _diag_exc:
                    print(f"{_INFO}  On-chain diag failed: {_diag_exc}")

                # If CLOB reports allowance=$0, ensure on-chain approvals
                if allowance == 0:
                    print(f"{_INFO}  Allowance $0 — verifying on-chain approvals…")
                    _USDC_ABI    = [
                        {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
                         "name":"approve","outputs":[{"name":"","type":"bool"}],
                         "stateMutability":"nonpayable","type":"function"},
                        {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
                         "name":"allowance","outputs":[{"name":"","type":"uint256"}],
                         "stateMutability":"view","type":"function"},
                    ]
                    from web3 import Web3
                    from eth_account import Account as _Acct
                    _rpc_candidates = [
                        os.environ.get("POLYGON_RPC_URL", ""),
                        "https://rpc.ankr.com/polygon/b77f5e0dd955373ca4c9e3d668d87d8217aaa907b55aa39d430ccc686b78fe22",
                        "https://polygon-bor-rpc.publicnode.com",
                        "https://polygon.drpc.org",
                        "https://1rpc.io/matic",
                        "https://polygon.llamarpc.com",
                    ]
                    _w3 = None
                    for _rpc in _rpc_candidates:
                        if not _rpc:
                            continue
                        try:
                            _candidate = Web3(Web3.HTTPProvider(_rpc, request_kwargs={"timeout": 10}))
                            _ = _candidate.eth.block_number
                            _w3 = _candidate
                            break
                        except Exception:
                            continue
                    if _w3 is None:
                        raise RuntimeError("All Polygon RPC endpoints failed")
                    _acct = _Acct.from_key(os.environ["POLY_PRIVATE_KEY"])
                    _usdc = _w3.eth.contract(
                        address=_w3.to_checksum_address(_USDC), abi=_USDC_ABI
                    )

                    def _ensure_approval(spender_addr, label):
                        _cur = _usdc.functions.allowance(
                            _w3.to_checksum_address(_acct.address),
                            _w3.to_checksum_address(spender_addr),
                        ).call()
                        print(f"{_INFO}  On-chain allowance ({label}): {_cur / 1e6:.2f} USDC")
                        if _cur < 5 * 10**6:
                            _tx = _usdc.functions.approve(
                                _w3.to_checksum_address(spender_addr), 60_000 * 10**6
                            ).build_transaction({
                                "from":                 _acct.address,
                                "nonce":                _w3.eth.get_transaction_count(_acct.address),
                                "gas":                  100_000,
                                "maxFeePerGas":         _w3.to_wei("150", "gwei"),
                                "maxPriorityFeePerGas": _w3.to_wei("30",  "gwei"),
                                "chainId":              137,
                            })
                            _stx = _w3.eth.account.sign_transaction(_tx, os.environ["POLY_PRIVATE_KEY"])
                            _txh = _w3.eth.send_raw_transaction(_stx.raw_transaction)
                            print(f"{_INFO}  approve({label}) tx: {_txh.hex()}")
                            _r = _w3.eth.wait_for_transaction_receipt(_txh, timeout=60)
                            if _r.status == 1:
                                print(f"{_PASS}  {label} approval confirmed")
                            else:
                                print(f"{_WARN}  {label} approve() reverted")
                        else:
                            print(f"{_INFO}  {label} already approved")

                    _ensure_approval(_CTF_TOKEN,       "CTF Contract")
                    _ensure_approval(_NEG_RISK_ADAPT,  "NegRiskAdapter")

                # Refresh Polymarket's cache using the ORDER client (sig_type=0 context)
                # so the balance/allowance pre-check matches the order's EOA context.
                try:
                    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                    _clob_client.update_balance_allowance(
                        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                    )
                    _bal_after = _clob_client.get_balance_allowance(
                        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                    )
                    print(f"{_INFO}  CLOB after refresh — balance: ${int(_bal_after.get('balance',0))/1e6:.2f}  allowance: ${int(_bal_after.get('allowance',0))/1e6:.2f}")
                except Exception as _uba_exc:
                    print(f"{_INFO}  update_balance_allowance: {_uba_exc}")

                # Use py_clob_client's post_order — handles Order dataclass serialization
                resp = _clob_client.post_order(_signed_order, OrderType.GTC)
                order_id = (resp.get("orderId") or resp.get("order_id") or "?") if isinstance(resp, dict) else str(resp)
                print(f"{_PASS}  Order ACCEPTED — orderId: {order_id}")
                print(f"{_INFO}  Full response: {json.dumps(resp, indent=4) if isinstance(resp, dict) else resp}")
                results["order_post"] = "PASS"
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
