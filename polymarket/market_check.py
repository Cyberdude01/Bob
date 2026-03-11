"""
Polymarket Market Check
=======================
Utility functions for inspecting live market state, balance, prices, and positions.

Usage (from /root):
  python -m polymarket.market_check
  python -m polymarket.market_check --env /etc/polymarket.env
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# ─── Path bootstrap (supports standalone invocation) ──────────────────────────
_THIS_DIR = Path(__file__).resolve().parent
_PKG_ROOT  = _THIS_DIR.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from polymarket.config import CLOB_API, DATA_API, GAMMA_API

# ─── Constants ────────────────────────────────────────────────────────────────
MARKETS_LIMIT = 20

# Read credentials at call-time so env loaded in main() is picked up
def _creds() -> Dict[str, str]:
    def _strip(v: str) -> str:
        return v.strip().strip('"').strip("'")
    return {
        "address":    _strip(os.environ.get("POLY_ADDRESS", "")),
        "api_key":    _strip(os.environ.get("POLY_API_KEY", "")),
        "api_secret": _strip(os.environ.get("POLY_API_SECRET", "")),
        "passphrase": _strip(os.environ.get("POLY_API_PASSPHRASE", "")),
    }


# ─── Auth ─────────────────────────────────────────────────────────────────────

def _l2_headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    c = _creds()
    if not c["api_key"] or not c["api_secret"]:
        return {}
    ts      = str(int(time.time()))
    message = ts + method.upper() + path + (body or "")
    try:
        secret_bytes = base64.b64decode(c["api_secret"])
    except Exception:
        secret_bytes = c["api_secret"].encode()
    sig = base64.b64encode(
        hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
    ).decode()
    return {
        "POLY-ADDRESS":    c["address"],
        "POLY-SIGNATURE":  sig,
        "POLY-TIMESTAMP":  ts,
        "POLY-API-KEY":    c["api_key"],
        "POLY-PASSPHRASE": c["passphrase"],
    }


# ─── Market Data ──────────────────────────────────────────────────────────────

def get_markets(**filters) -> List[Dict[str, Any]]:
    params = {
        "limit":  MARKETS_LIMIT,
        "active": True,
        "closed": False,
    }
    params.update(filters)
    response = requests.get(f"{GAMMA_API}/markets", params=params, timeout=15)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else data.get("markets", [])


def get_balance() -> float:
    path = "/balance-allowance"
    sig_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "1"))
    headers = {
        "Content-Type": "application/json",
        **_l2_headers("GET", path),
    }
    response = requests.get(
        f"{CLOB_API}{path}",
        params={"asset_type": 0, "signature_type": sig_type},
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    usdc = int(data.get("balance", 0)) / 1e6
    return usdc


def get_price(token_id: str) -> Dict[str, float]:
    def _get(endpoint: str, params: dict) -> dict:
        r = requests.get(f"{CLOB_API}{endpoint}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    midpoint = float(_get("/midpoint",     {"token_id": token_id}).get("mid",    0))
    best_ask = float(_get("/price",        {"token_id": token_id, "side": "BUY"}).get("price",  0))
    best_bid = float(_get("/price",        {"token_id": token_id, "side": "SELL"}).get("price", 0))
    spread   = float(_get("/spread",       {"token_id": token_id}).get("spread", 0))
    return {
        "midpoint": midpoint,
        "best_ask": best_ask,
        "best_bid": best_bid,
        "spread":   spread,
    }


def get_positions(address: Optional[str] = None) -> List[Dict[str, Any]]:
    addr = address or _creds()["address"]
    response = requests.get(
        f"{DATA_API}/positions",
        params={"user": addr},
        timeout=15,
    )
    response.raise_for_status()
    positions = response.json()
    print(f"{datetime.now().strftime('%H:%M:%S')} - {len(positions)} open positions")
    return positions


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _load_env(path: str) -> None:
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


def main() -> None:
    env_arg = next(
        (sys.argv[i + 1] for i, a in enumerate(sys.argv)
         if a == "--env" and i + 1 < len(sys.argv)),
        None,
    )
    _load_env(env_arg or "/etc/polymarket.env")

    print("\n=== Balance ===")
    try:
        bal = get_balance()
        print(f"  USDC: ${bal:.4f}")
    except Exception as exc:
        print(f"  ERROR: {exc}")

    print("\n=== Active 15-min Crypto Markets ===")
    now     = int(time.time())
    current = (now // 900) * 900
    ts_suffixes = [current, current + 900, current - 900]

    for base_slug in ("btc-updown-15m", "eth-updown-15m", "sol-updown-15m", "xrp-updown-15m"):
        found = False
        for ts in ts_suffixes:
            slug = f"{base_slug}-{ts}"
            try:
                r = requests.get(f"{GAMMA_API}/events", params={"slug": slug, "limit": 1}, timeout=10)
                r.raise_for_status()
                data = r.json()
                event = (data[0] if isinstance(data, list) and data
                         else data if isinstance(data, dict) and data.get("id") else None)
                if not event:
                    continue
                markets = event.get("markets", [])
                if not markets:
                    eid = str(event.get("id", ""))
                    mr = requests.get(f"{GAMMA_API}/markets", params={"event_id": eid, "limit": 20}, timeout=10)
                    markets = mr.json() if isinstance(mr.json(), list) else []
                if not markets:
                    continue
                m = markets[0]
                tokens = m.get("tokens") or m.get("clobTokenIds") or []
                # tokens may be dicts or a JSON string
                if isinstance(tokens, str):
                    try:
                        tokens = json.loads(tokens)
                    except Exception:
                        tokens = []
                up_tok   = next((t for t in tokens if isinstance(t, dict) and t.get("outcome", "").upper() == "UP"),   None)
                down_tok = next((t for t in tokens if isinstance(t, dict) and t.get("outcome", "").upper() == "DOWN"), None)
                print(f"  {slug}")
                print(f"    end       : {m.get('endDate', '?')[:19]}")
                if up_tok:
                    print(f"    UP  token : {up_tok.get('token_id') or up_tok.get('tokenId', '?')}")
                if down_tok:
                    print(f"    DOWN token: {down_tok.get('token_id') or down_tok.get('tokenId', '?')}")
                found = True
                break
            except Exception as exc:
                continue
        if not found:
            print(f"  {base_slug}: no active market")

    print("\n=== Positions ===")
    try:
        get_positions()
    except Exception as exc:
        print(f"  ERROR: {exc}")


if __name__ == "__main__":
    main()
