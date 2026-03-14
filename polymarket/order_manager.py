"""
Polymarket Order Manager
========================
Long-running daemon that continuously manages order placement in two modes:

1. Directional orders (trigger != 'pre_order')
   Pulled from signals.json every SIGNAL_REFRESH_SEC. Each signal is placed
   as a GTC limit order at the live best-ask.  Open orders are monitored
   every tick; those that drift more than REPRICE_THRESHOLD from the current
   live price are cancelled and re-placed automatically.

2. Pre-orders (pre-open window)
   In the final PRE_ORDER_WINDOW_SEC of each 15-minute candle the daemon
   places GTC limit orders for the *next* candle's market (next_market=True
   token lookup).  Dedup key includes the target candle's Unix timestamp so
   each candle fires exactly once.

Tick interval   : TICK_SEC          (default 30 s)
Signal refresh  : SIGNAL_REFRESH_SEC (default 5 min, via git fetch origin/main)

State files (data_exports/)
---------------------------
open_orders.json  — orders currently live on the CLOB
executed.json     — filled / completed orders (shared with trade_executor)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

# ── Path bootstrap ────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).resolve().parent
_PKG_ROOT  = _THIS_DIR.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from polymarket.config import CLOB_API, GAMMA_API
from polymarket.trade_executor import (
    _load_env,
    _load_json,
    _build_clob_client,
    _get_balance,
    _get_token_id,
    _get_live_price,
    _submit_order,
    _load_executed,
    _save_executed,
    _dedup_key,
    _check_signals_freshness,
    PRE_ORDER_WINDOW_SEC,
)

# ── Tuning ────────────────────────────────────────────────────────────────────
TICK_SEC           = 30     # main loop sleep interval
SIGNAL_REFRESH_SEC = 300    # re-pull signals from git every 5 min
REPRICE_THRESHOLD  = 0.03   # cancel & re-place if live price drifts > 3 cents

# ── State paths ───────────────────────────────────────────────────────────────
_DATA_DIR         = _PKG_ROOT / "data_exports"
_OPEN_ORDERS_FILE = _DATA_DIR / "open_orders.json"
_SIGNALS_FILE     = _DATA_DIR / "signals.json"
_MARKETS_FILE     = _DATA_DIR / "markets.json"

_ET = ZoneInfo("America/New_York")


# ─── open_orders helpers ──────────────────────────────────────────────────────

def _load_open_orders() -> Dict[str, Any]:
    if not _OPEN_ORDERS_FILE.exists():
        return {}
    try:
        return json.loads(_OPEN_ORDERS_FILE.read_text())
    except Exception:
        return {}


def _save_open_orders(orders: Dict[str, Any]) -> None:
    _OPEN_ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OPEN_ORDERS_FILE.write_text(json.dumps(orders, indent=2))


# ─── CLOB order management ────────────────────────────────────────────────────

def _check_order_status(client, order_id: str) -> str:
    """Returns 'LIVE', 'MATCHED', 'FILLED', 'CANCELLED', or 'UNKNOWN'."""
    try:
        order = client.get_order(order_id)
        if isinstance(order, dict):
            return str(order.get("status", "UNKNOWN")).upper()
    except Exception:
        pass
    # Public REST fallback
    try:
        r = requests.get(f"{CLOB_API}/order/{order_id}", timeout=10)
        if r.ok:
            return str(r.json().get("status", "UNKNOWN")).upper()
    except Exception:
        pass
    return "UNKNOWN"


def _cancel_order(client, order_id: str) -> bool:
    try:
        client.cancel_order(order_id)
        return True
    except Exception:
        try:
            client.cancel_orders([order_id])
            return True
        except Exception:
            return False


# ─── Signal refresh ───────────────────────────────────────────────────────────

def _refresh_signals_from_git() -> bool:
    """Pull fresh signals.json + markets.json from origin/main."""
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=_PKG_ROOT, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "origin/main", "--",
             "data_exports/signals.json", "data_exports/markets.json"],
            cwd=_PKG_ROOT, check=True, capture_output=True,
        )
        return True
    except Exception as exc:
        print(f"[order_manager] WARN: git fetch failed: {exc}")
        return False


# ─── Order placement ──────────────────────────────────────────────────────────

def _place_orders(
    client,
    signals: List[Dict[str, Any]],
    markets_raw: Dict[str, Any],
    signals_updated: str,
    open_orders: Dict[str, Any],
    executed: Dict[str, Any],
    is_pre_order_pass: bool,
    balance_ref: List[float],
    target_candle_ts: Optional[int] = None,
) -> None:
    """
    Place GTC limit orders for each signal not yet executed or open.

    is_pre_order_pass=False → directional orders for current candle.
    is_pre_order_pass=True  → pre-orders for next candle; requires target_candle_ts.
    """
    for sig in signals:
        slug       = sig.get("slug", "")
        symbol     = sig.get("symbol", "")
        outcome    = sig.get("outcome", "")
        side       = sig.get("side", "BUY")
        size       = float(sig.get("size", 5.0))
        trigger    = sig.get("trigger", "directional")
        confidence = float(sig.get("confidence", 0))

        # Directional pass: skip any explicit pre_order signals in the file
        if not is_pre_order_pass and trigger == "pre_order":
            continue

        # Pre-order pass: require a valid target candle
        if is_pre_order_pass:
            if target_candle_ts is None:
                continue
            dedup = f"pre_order:{slug}:{outcome}:{target_candle_ts}"
        else:
            dedup = _dedup_key(slug, outcome, signals_updated)

        # Skip if already executed or already have an open order
        if dedup in executed:
            continue
        if any(o.get("signal_key") == dedup for o in open_orders.values()):
            continue

        token_id = _get_token_id(slug, outcome, markets_raw, next_market=is_pre_order_pass)
        if not token_id:
            print(f"[order_manager] SKIP {symbol} {outcome} — token_id not found")
            continue

        live_price = _get_live_price(token_id, side)
        if not live_price:
            print(f"[order_manager] SKIP {symbol} {outcome} — no live price")
            continue

        if balance_ref[0] < size:
            print(f"[order_manager] SKIP {symbol} {outcome} — low balance ${balance_ref[0]:.2f}")
            continue

        shares = round(size / live_price, 4)
        tag = "PRE" if is_pre_order_pass else trigger

        try:
            resp     = _submit_order(client, token_id, live_price, shares, side)
            order_id = resp.get("orderId") or resp.get("order_id") or ""
            if not order_id:
                print(f"[order_manager] ERROR {symbol} {outcome} — no orderId: {resp}")
                continue

            open_orders[order_id] = {
                "slug":         slug,
                "symbol":       symbol,
                "outcome":      outcome,
                "trigger":      tag,
                "token_id":     token_id,
                "price":        live_price,
                "size_usdc":    size,
                "shares":       shares,
                "signal_key":   dedup,
                "placed_at_ts": int(time.time()),
                "placed_at":    datetime.now(_ET).strftime("%Y-%m-%d %I:%M:%S %p ET"),
                "is_pre_order": is_pre_order_pass,
            }
            balance_ref[0] -= size
            print(
                f"[order_manager] PLACED {symbol} {outcome} [{tag}] "
                f"orderId={order_id} price={live_price:.4f} "
                f"size=${size:.2f} conf={confidence:.3f}"
            )
        except Exception as exc:
            print(f"[order_manager] ERROR placing {symbol} {outcome}: {exc}")


# ─── Monitor & reprice open orders ────────────────────────────────────────────

def _monitor_open_orders(
    client,
    open_orders: Dict[str, Any],
    executed: Dict[str, Any],
) -> None:
    """Check fill status for all open orders. Cancel & queue reprice for drifted ones."""
    to_cancel: List[str] = []

    for order_id, meta in list(open_orders.items()):
        status = _check_order_status(client, order_id)

        if status in ("FILLED", "MATCHED"):
            ts_now = datetime.now(_ET).strftime("%Y-%m-%d %I:%M:%S %p ET")
            print(f"[order_manager] FILLED  {meta['symbol']} {meta['outcome']} orderId={order_id}")
            executed[meta["signal_key"]] = {
                "slug":         meta["slug"],
                "symbol":       meta["symbol"],
                "outcome":      meta["outcome"],
                "trigger":      meta["trigger"],
                "price":        meta["price"],
                "size":         meta["size_usdc"],
                "order_id":     order_id,
                "submitted_at": meta.get("placed_at", ts_now),
                "filled_at":    ts_now,
            }
            del open_orders[order_id]
            continue

        if status == "CANCELLED":
            print(f"[order_manager] EXPIRED {meta['symbol']} {meta['outcome']} orderId={order_id} — removing")
            del open_orders[order_id]
            continue

        # LIVE or UNKNOWN — check for price drift
        live_price = _get_live_price(meta["token_id"], "BUY")
        if live_price and abs(live_price - meta["price"]) > REPRICE_THRESHOLD:
            print(
                f"[order_manager] REPRICE {meta['symbol']} {meta['outcome']} "
                f"old={meta['price']:.4f} → new={live_price:.4f} "
                f"drift={abs(live_price - meta['price']):.4f}"
            )
            to_cancel.append(order_id)

    # Cancel drifted orders — they'll be re-placed next tick (not in executed, not in open_orders)
    for order_id in to_cancel:
        if order_id not in open_orders:
            continue
        if _cancel_order(client, order_id):
            del open_orders[order_id]
            print(f"[order_manager] CANCELLED for reprice orderId={order_id}")
        else:
            print(f"[order_manager] WARN: could not cancel orderId={order_id}")


# ─── Main loop ────────────────────────────────────────────────────────────────

def run() -> None:
    _load_env("/etc/polymarket.env")

    print(f"\n{'='*60}")
    print(f"  Polymarket Order Manager  [LIVE]")
    print(f"  {datetime.now(_ET).strftime('%Y-%m-%d %I:%M:%S %p ET')}")
    print(f"  tick={TICK_SEC}s  refresh={SIGNAL_REFRESH_SEC}s  reprice_threshold={REPRICE_THRESHOLD}")
    print(f"{'='*60}\n")

    client      = _build_clob_client()
    balance_ref = [_get_balance(client)]
    print(f"[order_manager] USDC balance: ${balance_ref[0]:.4f}")

    open_orders: Dict[str, Any] = _load_open_orders()
    print(f"[order_manager] open orders on resume: {len(open_orders)}")

    signals: List[Dict[str, Any]] = []
    markets_raw: Dict[str, Any]   = {"data": {}}
    signals_updated = ""
    last_signal_refresh = 0.0

    while True:
        try:
            now    = time.time()
            ts_str = datetime.now(_ET).strftime("%H:%M:%S ET")

            # ── Signal refresh every 5 min ────────────────────────────────
            if now - last_signal_refresh >= SIGNAL_REFRESH_SEC:
                print(f"[order_manager] {ts_str} — refreshing signals from origin/main")
                _refresh_signals_from_git()
                try:
                    raw             = _load_json(_SIGNALS_FILE)
                    signals_updated = raw.get("updated", "")
                    freshness_err   = _check_signals_freshness(signals_updated)
                    if freshness_err:
                        print(f"[order_manager] WARN: {freshness_err}")
                        signals = []
                    else:
                        signals = raw.get("data", [])
                        print(f"[order_manager] {len(signals)} signals (updated: {signals_updated})")
                except Exception as exc:
                    print(f"[order_manager] ERROR loading signals: {exc}")
                    signals = []

                try:
                    markets_raw = _load_json(_MARKETS_FILE)
                except Exception:
                    markets_raw = {"data": {}}

                balance_ref[0] = _get_balance(client)
                print(f"[order_manager] USDC balance: ${balance_ref[0]:.4f}")
                last_signal_refresh = now

            # ── Monitor open orders (fills + reprice) ─────────────────────
            executed = _load_executed()
            if open_orders:
                _monitor_open_orders(client, open_orders, executed)
                _save_open_orders(open_orders)
                _save_executed(executed)

            # ── Place directional orders ──────────────────────────────────
            if signals and signals_updated:
                _place_orders(
                    client, signals, markets_raw, signals_updated,
                    open_orders, executed,
                    is_pre_order_pass=False,
                    balance_ref=balance_ref,
                )
                _save_open_orders(open_orders)
                _save_executed(executed)

            # ── Pre-order window (last 5 min of candle) ───────────────────
            now_i            = int(time.time())
            time_until_next  = 900 - (now_i % 900)
            if time_until_next <= PRE_ORDER_WINDOW_SEC and signals and signals_updated:
                target_candle_ts = ((now_i // 900) + 1) * 900
                mins_to_open     = time_until_next / 60
                print(f"[order_manager] {ts_str} — pre-order window open "
                      f"(T-{mins_to_open:.1f}m, target candle {target_candle_ts})")
                _place_orders(
                    client, signals, markets_raw, signals_updated,
                    open_orders, executed,
                    is_pre_order_pass=True,
                    balance_ref=balance_ref,
                    target_candle_ts=target_candle_ts,
                )
                _save_open_orders(open_orders)
                _save_executed(executed)

        except KeyboardInterrupt:
            print("\n[order_manager] Interrupted — saving state and exiting.")
            _save_open_orders(open_orders)
            break
        except Exception as exc:
            print(f"[order_manager] UNHANDLED ERROR: {exc}")
            traceback.print_exc()

        time.sleep(TICK_SEC)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
