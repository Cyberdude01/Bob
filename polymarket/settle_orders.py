"""
Polymarket Order Settler
========================
After the trade executor places orders, this script:

  1. Checks every unsettled order in data_exports/executed.json.
  2. Waits until 3 minutes after the 15-min market close time.
  3. Reads the CLOB last-trade-price for the order's token_id:
       price >= 0.99  →  WIN   (shares pay out $1 each)
       price <= 0.01  →  LOSS  (shares expire worthless)
       in between     →  not yet resolved, try again next run
  4. Calculates PnL:
       WIN:  total_return = shares * 1.0
             pnl          = total_return - size   (capital + profit)
       LOSS: total_return = 0.0
             pnl          = -size                 (full capital lost)
  5. Writes reports/live_orders.md (open positions + settled history).
  6. Commits executed.json + live_orders.md and pushes to origin/main.

Run via: python3 -m polymarket.settle_orders
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests

_ET = ZoneInfo("America/New_York")

_THIS_DIR      = Path(__file__).resolve().parent
_PKG_ROOT      = _THIS_DIR.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from .config import CLOB_API

_DATA_DIR      = _PKG_ROOT / "data_exports"
_EXECUTED_FILE = _DATA_DIR / "executed.json"
_REPORTS_DIR   = _PKG_ROOT / "reports"
_LIVE_ORDERS   = _REPORTS_DIR / "live_orders.md"

# Minutes to wait after market close before checking resolution
SETTLE_BUFFER_MINUTES = 3


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


def _load_executed() -> Dict[str, Any]:
    if not _EXECUTED_FILE.exists():
        return {}
    try:
        return json.loads(_EXECUTED_FILE.read_text())
    except Exception:
        return {}


def _save_executed(executed: Dict[str, Any]) -> None:
    _EXECUTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    _EXECUTED_FILE.write_text(json.dumps(executed, indent=2))


def _parse_et_ts(ts_str: str) -> Optional[datetime]:
    """Parse 'YYYY-MM-DD HH:MM:SS AM/PM ET' → UTC-aware datetime."""
    cleaned = ts_str.strip()
    for label in (" ET", " EST", " EDT"):
        if cleaned.endswith(label):
            cleaned = cleaned[: -len(label)].strip()
            break
    for fmt in ("%Y-%m-%d %I:%M:%S %p", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(cleaned, fmt)
            # ET ≈ UTC-5 (conservative; covers both EST and EDT)
            return naive.replace(tzinfo=timezone.utc) + timedelta(hours=5)
        except ValueError:
            continue
    return None


def _market_close_utc(submitted_at: str) -> Optional[datetime]:
    """
    The 15-min market the order was placed in closes at the next
    15-min boundary after submission.
    """
    dt = _parse_et_ts(submitted_at)
    if dt is None:
        return None
    ts = int(dt.timestamp())
    close_ts = ((ts // 900) + 1) * 900
    return datetime.fromtimestamp(close_ts, tz=timezone.utc)


def _check_token_price(token_id: str) -> Optional[float]:
    """Return last-trade-price for a token, or None on failure."""
    try:
        r = requests.get(
            f"{CLOB_API}/last-trade-price",
            params={"token_id": token_id},
            timeout=10,
        )
        r.raise_for_status()
        price = float(r.json().get("price", -1))
        return price if price >= 0 else None
    except Exception:
        return None


# ─── Report writer ────────────────────────────────────────────────────────────

def _write_live_orders(executed: Dict[str, Any]) -> None:
    """Rebuild reports/live_orders.md from current executed.json."""
    _REPORTS_DIR.mkdir(exist_ok=True)
    now_str = datetime.now(_ET).strftime("%Y-%m-%d %I:%M %p ET")

    open_orders    = [e for e in executed.values() if not e.get("settled")]
    settled_orders = [e for e in executed.values() if e.get("settled")]

    open_orders.sort(   key=lambda e: e.get("submitted_at", ""), reverse=True)
    settled_orders.sort(key=lambda e: e.get("submitted_at", ""), reverse=True)

    lines = [
        "# Live Orders",
        "",
        f"*Updated: {now_str}*",
        "",
    ]

    # ── Open Positions ─────────────────────────────────────────────────────
    lines += [
        "## Open Positions",
        "",
        "| Submitted (ET) | Symbol | Outcome | Trigger | Entry Price | Shares | Stake | Order ID |",
        "|----------------|--------|---------|---------|-------------|--------|-------|----------|",
    ]
    if open_orders:
        for e in open_orders:
            ep     = float(e.get("price", 0) or 0)
            sz     = float(e.get("size",  0) or 0)
            shares = e.get("shares") or (round(sz / ep, 4) if ep > 0 else "?")
            oid    = str(e.get("order_id", "?"))[:12]
            lines.append(
                f"| {e.get('submitted_at','?')} "
                f"| {e.get('symbol','?')} "
                f"| **{e.get('outcome','?')}** "
                f"| `{e.get('trigger','?')}` "
                f"| {ep:.4f} "
                f"| {shares} "
                f"| ${sz:.2f} "
                f"| `{oid}` |"
            )
    else:
        lines.append("| — | — | — | — | — | — | — | — |")

    lines.append("")

    # ── Settled Trades ─────────────────────────────────────────────────────
    lines += [
        "## Settled Trades",
        "",
        "| Submitted (ET) | Symbol | Outcome | Trigger | Entry Price | Shares | Stake | Result | PnL | Total Return |",
        "|----------------|--------|---------|---------|-------------|--------|-------|--------|-----|--------------|",
    ]
    if settled_orders:
        for e in settled_orders:
            ep           = float(e.get("price", 0) or 0)
            sz           = float(e.get("size",  0) or 0)
            shares       = e.get("shares") or (round(sz / ep, 4) if ep > 0 else "?")
            result       = e.get("result", "?")
            pnl          = float(e.get("pnl", 0) or 0)
            total_return = float(e.get("total_return", 0) or 0)
            result_fmt   = "WIN" if result == "WIN" else "LOSS"
            pnl_str      = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            lines.append(
                f"| {e.get('submitted_at','?')} "
                f"| {e.get('symbol','?')} "
                f"| **{e.get('outcome','?')}** "
                f"| `{e.get('trigger','?')}` "
                f"| {ep:.4f} "
                f"| {shares} "
                f"| ${sz:.2f} "
                f"| {result_fmt} "
                f"| {pnl_str} "
                f"| ${total_return:.2f} |"
            )
    else:
        lines.append("| — | — | — | — | — | — | — | — | — | — |")

    # ── Summary ────────────────────────────────────────────────────────────
    total_traded  = sum(float(e.get("size", 0) or 0) for e in settled_orders + open_orders)
    realized_pnl  = sum(float(e.get("pnl",  0) or 0) for e in settled_orders)
    open_at_risk  = sum(float(e.get("size", 0) or 0) for e in open_orders)
    wins          = sum(1 for e in settled_orders if e.get("result") == "WIN")
    losses        = sum(1 for e in settled_orders if e.get("result") == "LOSS")
    pnl_sign      = "+" if realized_pnl >= 0 else ""

    lines += [
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total Trades | {len(settled_orders) + len(open_orders)} |",
        f"| Open | {len(open_orders)} (${open_at_risk:.2f} at risk) |",
        f"| Settled | {len(settled_orders)} — {wins}W / {losses}L |",
        f"| Total Staked | ${total_traded:.2f} |",
        f"| Realized PnL | {pnl_sign}${realized_pnl:.2f} |",
        "",
    ]

    _LIVE_ORDERS.write_text("\n".join(lines))
    print(f"[settle] live_orders.md written ({len(open_orders)} open, {len(settled_orders)} settled)")


# ─── Git ──────────────────────────────────────────────────────────────────────

def _git_commit_push(message: str) -> None:
    try:
        subprocess.run(
            ["git", "add",
             "reports/live_orders.md",
             "data_exports/executed.json"],
            cwd=_PKG_ROOT, check=True, capture_output=True,
        )
        # Only commit if there's actually something staged
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=_PKG_ROOT, capture_output=True,
        )
        if diff.returncode == 0:
            print("[settle] Nothing to commit — no changes detected")
            return

        subprocess.run(
            ["git", "commit", "-m", message,
             "--author", "Polymarket Feed <polymarket-feed@bot>"],
            cwd=_PKG_ROOT, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            cwd=_PKG_ROOT, check=True, capture_output=True,
        )
        print(f"[settle] Pushed to origin/main: {message}")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace")[:300] if e.stderr else ""
        print(f"[settle] Git error: {stderr}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run() -> None:
    ts_run = datetime.now(_ET).strftime("%Y-%m-%d %I:%M:%S %p ET")
    print(f"\n[settle] Order settler — {ts_run}")

    executed = _load_executed()
    if not executed:
        print("[settle] No executed orders found.")
        _write_live_orders(executed)
        return

    now_utc       = datetime.now(timezone.utc)
    settled_count = 0
    checked_count = 0

    for key, entry in executed.items():
        if entry.get("settled"):
            continue  # Already done

        submitted_at = entry.get("submitted_at", "")
        token_id     = entry.get("token_id")
        close_time   = _market_close_utc(submitted_at)

        # Wait until market close + buffer before checking
        settle_after = (close_time + timedelta(minutes=SETTLE_BUFFER_MINUTES)
                        if close_time else None)
        if settle_after is None or now_utc < settle_after:
            close_str = close_time.strftime("%H:%MZ") if close_time else "?"
            print(f"[settle] {entry.get('symbol')} {entry.get('outcome')} — "
                  f"market closes {close_str}, not yet due")
            continue

        if not token_id:
            print(f"[settle] {entry.get('symbol')} {entry.get('outcome')} — "
                  f"no token_id stored (placed before settlement tracking), skipping")
            continue

        checked_count += 1
        price = _check_token_price(token_id)
        sym   = entry.get("symbol", "?")
        out   = entry.get("outcome", "?")
        print(f"[settle] {sym} {out} — last-trade-price: {price}")

        if price is None:
            print(f"[settle]   → price fetch failed, will retry next run")
            continue

        size  = float(entry.get("size",  5.0) or 5.0)
        ep    = float(entry.get("price", 0)   or 0)
        shares = float(
            entry.get("shares")
            or (round(size / ep, 4) if ep > 0 else 0)
        )

        if price >= 0.99:
            # WIN — each share pays out $1.00
            total_return = round(shares * 1.0, 4)
            pnl          = round(total_return - size, 4)
            result       = "WIN"
        elif price <= 0.01:
            # LOSS — shares expire worthless, full capital lost
            total_return = 0.0
            pnl          = round(-size, 4)
            result       = "LOSS"
        else:
            # Market not yet resolved
            print(f"[settle]   → price {price:.4f} not yet at 0 or 1, market still resolving")
            continue

        entry["settled"]      = True
        entry["result"]       = result
        entry["pnl"]          = pnl
        entry["total_return"] = total_return
        entry["settled_at"]   = datetime.now(_ET).strftime("%Y-%m-%d %I:%M:%S %p ET")
        settled_count += 1

        print(f"[settle]   → {result}  pnl={pnl:+.2f}  total_return=${total_return:.2f}")

    _save_executed(executed)
    _write_live_orders(executed)

    ts_label = datetime.now(_ET).strftime("%Y-%m-%dT%H:%M ET")
    if settled_count > 0:
        _git_commit_push(f"settle: {settled_count} order(s) settled — {ts_label}")
    else:
        # Still push so live_orders.md stays current (open positions visible on GitHub)
        _git_commit_push(f"orders: live_orders updated — {ts_label}")

    print(f"[settle] Done — {settled_count} newly settled, {checked_count} checked\n")


def main() -> None:
    env_arg = next(
        (sys.argv[i + 1] for i, a in enumerate(sys.argv)
         if a == "--env" and i + 1 < len(sys.argv)),
        None,
    )
    _load_env(env_arg or "/etc/polymarket.env")
    run()


if __name__ == "__main__":
    main()
