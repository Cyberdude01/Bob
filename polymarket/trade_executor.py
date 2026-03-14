"""
Polymarket Trade Executor
=========================
Reads pending signals from data_exports/signals.json, fetches live prices
from the CLOB, and submits orders via py_clob_client.

Usage (from /root):
  # Dry run — shows what would be traded, no orders sent:
  python -m polymarket.trade_executor

  # Live — REAL MONEY:
  python -m polymarket.trade_executor --execute

  # Custom env file:
  python -m polymarket.trade_executor --env /path/to/my.env

Deduplication:
  Executed signals are recorded in data_exports/executed.json keyed by
  "{slug}:{outcome}:{signals_updated_ts}".  Re-running the executor for
  the same signals.json snapshot is therefore safe.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# ─── Path bootstrap ───────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).resolve().parent
_PKG_ROOT  = _THIS_DIR.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

CLOB_API  = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# ─── Paths ────────────────────────────────────────────────────────────────────
_DATA_DIR      = _PKG_ROOT / "data_exports"
_SIGNALS_FILE  = _DATA_DIR / "signals.json"
_MARKETS_FILE  = _DATA_DIR / "markets.json"
_EXECUTED_FILE = _DATA_DIR / "executed.json"

# ─── Constants ────────────────────────────────────────────────────────────────

# Signals older than this are rejected — prevents stale signals trading future markets
MAX_SIGNAL_AGE_HOURS = 4

# Pre-order window: only execute pre_order signals within this many seconds of the
# next 15-minute market open (i.e. during the last 5 minutes of the current candle)
PRE_ORDER_WINDOW_SEC = 300   # 5 minutes

# ─── Helpers ──────────────────────────────────────────────────────────────────

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


def _load_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    return json.loads(path.read_text())


def _save_executed(executed: Dict[str, Any]) -> None:
    _EXECUTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    _EXECUTED_FILE.write_text(json.dumps(executed, indent=2))


def _load_executed() -> Dict[str, Any]:
    if not _EXECUTED_FILE.exists():
        return {}
    try:
        return json.loads(_EXECUTED_FILE.read_text())
    except Exception:
        return {}


def _dedup_key(slug: str, outcome: str, updated_ts: str) -> str:
    return f"{slug}:{outcome}:{updated_ts}"


def _parse_signals_updated(updated_str: str) -> Optional[datetime]:
    """
    Parse the 'updated' field from signals.json, e.g. '2026-03-10 07:17:05 AM ET'.
    Returns a UTC-aware datetime, or None if parsing fails.
    Eastern Time is approximated as UTC-5 (conservative; covers both EST and EDT).
    """
    # Strip trailing timezone label and parse
    cleaned = updated_str.strip()
    for tz_label in (" ET", " EST", " EDT", " UTC"):
        if cleaned.endswith(tz_label):
            cleaned = cleaned[: -len(tz_label)].strip()
            break

    for fmt in ("%Y-%m-%d %I:%M:%S %p", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(cleaned, fmt)
            # Treat ET as UTC-5 (conservative; worst-case is 1h off in EDT)
            return naive.replace(tzinfo=timezone.utc) + timedelta(hours=5)
        except ValueError:
            continue
    return None


def _check_signals_freshness(updated_str: str) -> Optional[str]:
    """
    Returns an error message string if signals are too old, else None.
    """
    signals_dt = _parse_signals_updated(updated_str)
    if signals_dt is None:
        return f"Could not parse signals 'updated' timestamp: {updated_str!r}"

    age = datetime.now(timezone.utc) - signals_dt
    if age > timedelta(hours=MAX_SIGNAL_AGE_HOURS):
        hours_old = age.total_seconds() / 3600
        return (
            f"signals.json is {hours_old:.1f}h old (updated: {updated_str}). "
            f"Max allowed age is {MAX_SIGNAL_AGE_HOURS}h. "
            f"Refusing to execute — stale signals may target wrong market dates."
        )
    return None


# ─── Market token lookup ───────────────────────────────────────────────────────

def _get_token_id(
    slug: str,
    outcome: str,
    markets_data: Dict[str, Any],
    next_market: bool = False,
) -> Optional[str]:
    """
    Resolve the CLOB token_id for a given slug + outcome (UP/DOWN).
    Primary: look up condition_id from markets.json then query CLOB /markets/{cid}.
    Fallback: scan Gamma /events by slug timestamp candidates.

    next_market=True targets the *next* 15-minute slot (for pre_order signals).
    """
    # Find condition_id from markets.json (only useful for current-market signals)
    if not next_market:
        condition_id: Optional[str] = None
        for sym_data in markets_data.get("data", {}).values():
            if sym_data.get("slug", "").split("-")[0] in slug:
                condition_id = sym_data.get("condition_id")
                break

        if condition_id:
            try:
                r = requests.get(f"{CLOB_API}/markets/{condition_id}", timeout=10)
                r.raise_for_status()
                cd = r.json()
                for tok in (cd.get("tokens") or []):
                    if str(tok.get("outcome", "")).upper() == outcome.upper():
                        return str(tok.get("token_id", ""))
            except Exception:
                pass

    # Gamma events slug scan
    now     = int(time.time())
    current = (now // 900) * 900
    if next_market:
        # Target the next boundary and the one after as fallback
        ts_candidates = (current + 900, current + 1800)
    else:
        ts_candidates = (current, current + 900, current - 900)
    for ts in ts_candidates:
        full_slug = f"{slug}-{ts}"
        try:
            r = requests.get(f"{GAMMA_API}/events", params={"slug": full_slug, "limit": 1}, timeout=10)
            r.raise_for_status()
            data  = r.json()
            event = (data[0] if isinstance(data, list) and data
                     else data if isinstance(data, dict) and data.get("id") else None)
            if not event:
                continue
            markets_list = event.get("markets", [])
            if not markets_list:
                eid = str(event.get("id", ""))
                mr  = requests.get(f"{GAMMA_API}/markets", params={"event_id": eid, "limit": 20}, timeout=10)
                mr.raise_for_status()
                markets_list = mr.json() if isinstance(mr.json(), list) else []
            if not markets_list:
                continue
            m        = markets_list[0]
            raw_ids  = m.get("clobTokenIds") or "[]"
            raw_outs = m.get("outcomes") or "[]"
            if isinstance(raw_ids, str):
                raw_ids = json.loads(raw_ids)
            if isinstance(raw_outs, str):
                raw_outs = json.loads(raw_outs)
            for tid, out in zip(raw_ids, raw_outs):
                if str(out).upper() == outcome.upper():
                    return str(tid)
        except Exception:
            continue

    return None


def _get_live_price(token_id: str, side: str) -> Optional[float]:
    """
    Fetch best_ask (for BUY) or best_bid (for SELL) from CLOB.
    Falls back to last-trade-price if the order book returns 0.
    """
    try:
        clob_side = "BUY" if side.upper() == "BUY" else "SELL"
        r = requests.get(
            f"{CLOB_API}/price",
            params={"token_id": token_id, "side": clob_side},
            timeout=10,
        )
        r.raise_for_status()
        price = float(r.json().get("price", 0))
        if price > 0:
            return price
    except Exception:
        pass

    # Fallback: last-trade-price
    try:
        r = requests.get(f"{CLOB_API}/last-trade-price", params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        price = float(r.json().get("price", 0))
        if price > 0:
            return price
    except Exception:
        pass

    return None


# ─── Order submission ─────────────────────────────────────────────────────────

def _build_clob_client():
    from py_clob_client.client import ClobClient
    sig_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "1"))
    client   = ClobClient(
        CLOB_API,
        key            = os.environ["POLY_PRIVATE_KEY"],
        chain_id       = 137,
        signature_type = sig_type,
        funder         = os.environ["POLY_ADDRESS"],
    )
    client.set_api_creds(client.derive_api_key())
    return client


def _get_balance(client) -> float:
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    return int(bal.get("balance", 0)) / 1e6


def _submit_order(client, token_id: str, price: float, size: float, side: str) -> Dict[str, Any]:
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL

    clob_side = BUY if side.upper() == "BUY" else SELL
    try:
        order_args = OrderArgs(
            token_id = token_id,
            price    = round(price, 4),
            size     = size,
            side     = clob_side,
            neg_risk = False,
        )
    except TypeError:
        order_args = OrderArgs(
            token_id = token_id,
            price    = round(price, 4),
            size     = size,
            side     = clob_side,
        )

    signed = client.create_order(order_args)
    resp   = client.post_order(signed, OrderType.GTC)
    return resp if isinstance(resp, dict) else {"raw": str(resp)}


# ─── Main executor ────────────────────────────────────────────────────────────

def run(execute: bool = False) -> None:
    ts_run = datetime.now(_ET).strftime("%Y-%m-%d %I:%M:%S %p ET")
    print(f"\n{'='*60}")
    print(f"  Polymarket Trade Executor  {'[DRY-RUN]' if not execute else '[LIVE]'}")
    print(f"  {ts_run}")
    print(f"{'='*60}\n")

    # ── Load signals & markets ─────────────────────────────────────────────
    try:
        signals_raw = _load_json(_SIGNALS_FILE)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return

    signals_updated = signals_raw.get("updated", "unknown")
    signals: List[Dict[str, Any]] = signals_raw.get("data", [])
    print(f"signals.json updated : {signals_updated}")
    print(f"signals found        : {len(signals)}")

    # ── Staleness guard (temporarily disabled — re-enable after feed is confirmed live)
    # freshness_err = _check_signals_freshness(signals_updated)
    # if freshness_err:
    #     print(f"\nERROR: {freshness_err}")
    #     print("Update signals.json before re-running.\n")
    #     return

    try:
        markets_raw = _load_json(_MARKETS_FILE)
    except FileNotFoundError:
        markets_raw = {"data": {}}
        print("WARN: markets.json not found — will use Gamma fallback for token lookup")

    executed = _load_executed()
    print(f"already executed     : {len(executed)} entries\n")

    if not signals:
        print("No signals to process.")
        return

    # ── Initialise CLOB client & check balance ─────────────────────────────
    try:
        client  = _build_clob_client()
        balance = _get_balance(client)
        print(f"USDC balance         : ${balance:.4f}\n")
    except Exception as exc:
        print(f"ERROR initialising CLOB client: {exc}")
        return

    # ── Process each signal ────────────────────────────────────────────────
    submitted = 0
    skipped   = 0
    errors    = 0

    for sig in signals:
        slug     = sig.get("slug", "")
        symbol   = sig.get("symbol", "")
        outcome  = sig.get("outcome", "")       # UP / DOWN
        side     = sig.get("side", "BUY")       # BUY
        size     = float(sig.get("size", 5.0))
        trigger  = sig.get("trigger", "?")
        reason   = sig.get("reason", "")
        confidence = sig.get("confidence", 0)

        dedup_key = _dedup_key(slug, outcome, signals_updated)

        prefix = f"  [{symbol} {outcome} | {trigger}]"

        # Skip duplicates
        if dedup_key in executed:
            print(f"{prefix} SKIP (already executed at {executed[dedup_key].get('submitted_at','?')})")
            skipped += 1
            continue

        # Pre-order timing gate: only execute within the last 5 minutes of the current candle
        is_pre_order = (trigger == "pre_order")
        if is_pre_order:
            _now = int(time.time())
            _time_until_next = 900 - (_now % 900)
            if _time_until_next > PRE_ORDER_WINDOW_SEC:
                print(f"{prefix} SKIP — pre_order window not open ({_time_until_next:.0f}s until next market, window opens at T-{PRE_ORDER_WINDOW_SEC}s)")
                skipped += 1
                continue

        # Resolve token_id (pre_orders target the next 15-min slot)
        token_id = _get_token_id(slug, outcome, markets_raw, next_market=is_pre_order)
        if not token_id:
            print(f"{prefix} ERROR — could not resolve token_id for {slug} {outcome}")
            errors += 1
            continue

        # Fetch live price
        live_price = _get_live_price(token_id, side)
        if live_price is None or live_price <= 0:
            print(f"{prefix} ERROR — could not fetch live price for token {token_id[:12]}…")
            errors += 1
            continue

        # Skip near-resolved markets (price ≥ 0.95 means candle already closed/settling)
        if live_price >= 0.95:
            print(f"{prefix} SKIP — market already resolved (live_price={live_price:.4f} ≥ 0.95, no edge)")
            skipped += 1
            continue

        # Balance guard
        if balance < size:
            print(f"{prefix} SKIP — insufficient balance (${balance:.2f} < ${size:.2f})")
            skipped += 1
            continue

        print(f"{prefix}")
        print(f"    token     : {token_id[:16]}…")
        print(f"    live_price: {live_price:.4f}  size: ${size:.2f}")
        print(f"    confidence: {confidence:.3f}")
        print(f"    reason    : {reason[:100]}")

        # Convert dollar notional to share count (API expects shares, not dollars)
        shares = round(size / live_price, 4)

        if not execute:
            print(f"    --> DRY-RUN (would submit BUY {outcome} @ {live_price:.4f}, {shares} shares)")
            # Record as dry-run so we can see what would fire
        else:
            print(f"    --> SUBMITTING order (REAL MONEY)…")
            try:
                resp = _submit_order(client, token_id, live_price, shares, side)
                order_id = resp.get("orderId") or resp.get("order_id") or "?"
                print(f"    --> ACCEPTED  orderId={order_id}")

                # Deduct from local balance estimate
                balance -= size

                # Record execution
                executed[dedup_key] = {
                    "slug":         slug,
                    "symbol":       symbol,
                    "outcome":      outcome,
                    "trigger":      trigger,
                    "price":        live_price,
                    "size":         size,
                    "shares":       shares,
                    "token_id":     token_id,
                    "order_id":     order_id,
                    "submitted_at": datetime.now(_ET).strftime("%Y-%m-%d %I:%M:%S %p ET"),
                    "settled":      False,
                    "result":       None,
                    "pnl":          None,
                    "total_return": None,
                }
                _save_executed(executed)
                submitted += 1

            except Exception as exc:
                print(f"    --> ERROR submitting: {exc}")
                errors += 1
                continue

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  submitted : {submitted}")
    print(f"  skipped   : {skipped}  (already executed or low balance)")
    print(f"  errors    : {errors}")
    if not execute and (len(signals) - skipped - errors) > 0:
        print(f"\n  Re-run with --execute to submit real orders.")
    print()


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    execute = "--execute" in sys.argv
    env_arg = next(
        (sys.argv[i + 1] for i, a in enumerate(sys.argv)
         if a == "--env" and i + 1 < len(sys.argv)),
        None,
    )
    _load_env(env_arg or "/etc/polymarket.env")

    if execute:
        print("\033[93mWARNING: --execute flag set. Real orders will be submitted.\033[0m")
        print("Press Ctrl-C within 5 seconds to abort…")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(0)

    run(execute=execute)


if __name__ == "__main__":
    main()
