#!/usr/bin/env python3
"""
Polymarket 15-min Up/Down Backtester
=====================================
Loads BTC/ETH/SOL/XRP CSV data and simulates multiple signal strategies.
The CSV records are per-minute snapshots of active 15-min markets; the
final price in a window determines the winner (up_price → 1 = UP wins,
down_price → 1 = DOWN wins).

Usage:
  python3 backtest.py
  python3 backtest.py --symbol BTC
  python3 backtest.py --strategy all
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DATA_DIR = Path(__file__).parent / "data_exports"
SYMBOLS  = ["BTC", "ETH", "SOL", "XRP"]

# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class Candle:
    ts: str
    symbol: str
    condition_id: str
    up_price: float
    down_price: float
    elapsed_pct: float
    remaining_sec: float
    vol_bucket: str      # "HighVol" | "LowVol"
    trend_bucket: str    # "Trend"   | "Range"
    rv60: float
    eff60: float
    prob_up: float
    market_start_ts: str
    market_end_ts: str
    up_spread: float
    down_spread: float


@dataclass
class Window:
    """One resolved 15-min market window."""
    condition_id: str
    symbol: str
    market_start_ts: str
    market_end_ts: str = ""
    candles: List[Candle] = field(default_factory=list)
    winner: Optional[str] = None   # "UP" | "DOWN" | None (unresolved)

    def resolve(self) -> None:
        """
        Determine winner from the last candle: whichever token price is closest
        to 1.0 (i.e. > 0.9) wins.  If neither is clearly settled, mark None.
        """
        if not self.candles:
            return
        last = self.candles[-1]
        if last.up_price >= 0.90:
            self.winner = "UP"
        elif last.down_price >= 0.90:
            self.winner = "DOWN"
        # else: unresolved (market might still be live)


# ─── CSV loading ──────────────────────────────────────────────────────────────

def load_symbol(symbol: str) -> List[Window]:
    csv_path = DATA_DIR / f"{symbol}.csv"
    if not csv_path.exists():
        print(f"  [warn] {csv_path} not found, skipping {symbol}")
        return []

    raw: List[Candle] = []
    with open(csv_path, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                raw.append(Candle(
                    ts            = row["ts"],
                    symbol        = row["symbol"],
                    condition_id  = row["condition_id"],
                    up_price      = float(row["up_price"]  or 0),
                    down_price    = float(row["down_price"] or 0),
                    elapsed_pct   = float(row["elapsed_pct"] or 0),
                    remaining_sec = float(row["remaining_sec"] or 0),
                    vol_bucket    = row["vol_bucket"]   or "unknown",
                    trend_bucket  = row["trend_bucket"] or "unknown",
                    rv60          = float(row["rv60"]  or 0),
                    eff60         = float(row["eff60"] or 0),
                    prob_up       = float(row["prob_up"] or 0.5),
                    market_start_ts = row["market_start_ts"],
                    market_end_ts   = row["market_end_ts"],
                    up_spread     = float(row.get("up_spread",  0) or 0),
                    down_spread   = float(row.get("down_spread", 0) or 0),
                ))
            except (ValueError, KeyError):
                continue

    # Group into windows by condition_id
    by_cid: Dict[str, List[Candle]] = defaultdict(list)
    for c in raw:
        by_cid[c.condition_id].append(c)

    windows = []
    for cid, candles in by_cid.items():
        candles.sort(key=lambda c: c.ts)
        w = Window(
            condition_id    = cid,
            symbol          = candles[0].symbol,
            market_start_ts = candles[0].market_start_ts,
            market_end_ts   = candles[0].market_end_ts,
            candles         = candles,
        )
        w.resolve()
        windows.append(w)

    # Sort windows by market start time
    windows.sort(key=lambda w: w.market_start_ts)
    return windows


# ─── P&L calculation helper ───────────────────────────────────────────────────

def pnl(entry_price: float, outcome: str, winner: str, size: float = 5.0) -> float:
    """
    Binary market P&L:
      Win:  size * (1/entry_price - 1)   [receive $1 per token, paid entry_price]
      Loss: -size
    """
    if outcome == winner:
        return size * (1.0 / entry_price - 1.0)
    else:
        return -size


# ─── Strategies ───────────────────────────────────────────────────────────────

@dataclass
class TradeResult:
    strategy:   str
    symbol:     str
    condition_id: str
    outcome:    str
    entry_price: float
    winner:     str
    pl:         float
    elapsed_at_entry: float
    trigger_candle_ts: str
    bucket: str


def simulate_trend_follow(
    windows: List[Window],
    min_edge: float = 0.12,
    min_elapsed: float = 0.50,
    max_elapsed: float = 0.90,
    size: float = 5.0,
    one_trade_per_window: bool = True,
) -> List[TradeResult]:
    """
    Trend-follow: fire when trendBucket=Trend, elapsed in [min_elapsed, max_elapsed),
    |probUp - 0.5| >= min_edge.  By default only the FIRST qualifying candle per
    window fires (one_trade_per_window=True) to match the intended 5-min cron behaviour.
    """
    results = []
    for w in windows:
        if w.winner is None:
            continue
        traded = False
        for c in w.candles:
            if traded and one_trade_per_window:
                break
            if c.trend_bucket != "Trend":
                continue
            edge = abs(c.prob_up - 0.5)
            if edge < min_edge:
                continue
            if not (min_elapsed <= c.elapsed_pct < max_elapsed):
                continue
            outcome = "UP" if c.prob_up >= 0.5 else "DOWN"
            price   = c.up_price if outcome == "UP" else c.down_price
            if price <= 0:
                continue
            results.append(TradeResult(
                strategy          = "trend_follow",
                symbol            = w.symbol,
                condition_id      = w.condition_id,
                outcome           = outcome,
                entry_price       = price,
                winner            = w.winner,
                pl                = pnl(price, outcome, w.winner, size),
                elapsed_at_entry  = c.elapsed_pct,
                trigger_candle_ts = c.ts,
                bucket            = f"{c.vol_bucket}+{c.trend_bucket}",
            ))
            traded = True
    return results


def simulate_directional(
    windows: List[Window],
    min_elapsed: float = 0.67,
    min_edge: float = 0.25,
    size: float = 5.0,
    one_trade_per_window: bool = True,
) -> List[TradeResult]:
    """
    Directional 90pct: elapsed >= min_elapsed, |probUp - 0.5| >= min_edge.
    Takes the first qualifying candle per window.
    """
    results = []
    for w in windows:
        if w.winner is None:
            continue
        traded = False
        for c in w.candles:
            if traded and one_trade_per_window:
                break
            edge = abs(c.prob_up - 0.5)
            if edge < min_edge:
                continue
            if c.elapsed_pct < min_elapsed:
                continue
            outcome = "UP" if c.prob_up >= 0.5 else "DOWN"
            price   = c.up_price if outcome == "UP" else c.down_price
            if price <= 0:
                continue
            results.append(TradeResult(
                strategy          = "directional_90pct",
                symbol            = w.symbol,
                condition_id      = w.condition_id,
                outcome           = outcome,
                entry_price       = price,
                winner            = w.winner,
                pl                = pnl(price, outcome, w.winner, size),
                elapsed_at_entry  = c.elapsed_pct,
                trigger_candle_ts = c.ts,
                bucket            = f"{c.vol_bucket}+{c.trend_bucket}",
            ))
            traded = True
    return results


def simulate_pre_order(
    windows: List[Window],
    min_elapsed: float = 0.667,
    max_elapsed: float = 0.95,
    entry_price: float = 0.48,
    size: float = 5.0,
) -> List[TradeResult]:
    """
    Pre-order straddle: fire both UP and DOWN at entry_price for the NEXT window.
    We simulate this by finding windows preceded by a qualifying "pre-order" candle
    in the prior window, and recording 2 trades (UP + DOWN) at entry_price.
    Since we always buy both sides at 0.48, the net per window is always:
      Winner leg: size * (1/0.48 - 1) = +$5.4167
      Loser leg:  -size = -$5.00
      Net: +$0.4167 per qualified window
    """
    results = []
    # The pre-order is STRADDLE: one leg always wins, one always loses.
    # We just need to find windows that had a prior-window candle in range.
    # For simplicity, identify windows with at least one candle in [min_elapsed, max_elapsed)
    # and treat the *following* window as the traded window.
    triggered_windows = set()
    by_symbol_start: Dict[str, List[Window]] = defaultdict(list)
    for w in windows:
        by_symbol_start[w.symbol].append(w)

    for sym_windows in by_symbol_start.values():
        for i, w in enumerate(sym_windows[:-1]):
            # Check if current window had a candle triggering pre_order
            triggered = any(
                min_elapsed <= c.elapsed_pct < max_elapsed
                for c in w.candles
            )
            if not triggered:
                continue
            next_w = sym_windows[i + 1]
            if next_w.winner is None:
                continue
            # UP leg
            results.append(TradeResult(
                strategy          = "pre_order_UP",
                symbol            = next_w.symbol,
                condition_id      = next_w.condition_id,
                outcome           = "UP",
                entry_price       = entry_price,
                winner            = next_w.winner,
                pl                = pnl(entry_price, "UP", next_w.winner, size),
                elapsed_at_entry  = 0.0,
                trigger_candle_ts = w.candles[-1].ts if w.candles else "",
                bucket            = "pre_order",
            ))
            # DOWN leg
            results.append(TradeResult(
                strategy          = "pre_order_DOWN",
                symbol            = next_w.symbol,
                condition_id      = next_w.condition_id,
                outcome           = "DOWN",
                entry_price       = entry_price,
                winner            = next_w.winner,
                pl                = pnl(entry_price, "DOWN", next_w.winner, size),
                elapsed_at_entry  = 0.0,
                trigger_candle_ts = w.candles[-1].ts if w.candles else "",
                bucket            = "pre_order",
            ))
    return results


# ─── Edge-threshold sweep ─────────────────────────────────────────────────────

def sweep_trend_follow_threshold(
    windows: List[Window],
    thresholds: list | None = None,
) -> None:
    if thresholds is None:
        thresholds = [round(x * 0.02, 2) for x in range(5, 26)]  # 0.10 → 0.50

    print("\n=== Trend-Follow Edge-Threshold Sweep ===")
    print(f"{'Edge':>6} {'Trades':>7} {'Wins':>6} {'WinRate':>8} {'TotalPL':>10} {'AvgPL':>8} {'AvgEntry':>9}")
    for t in thresholds:
        trades = simulate_trend_follow(windows, min_edge=t)
        if not trades:
            continue
        wins   = sum(1 for r in trades if r.pl > 0)
        total  = sum(r.pl for r in trades)
        avg_pl = total / len(trades)
        avg_e  = sum(r.entry_price for r in trades) / len(trades)
        wr     = wins / len(trades) * 100
        print(f"{t:>6.2f} {len(trades):>7} {wins:>6} {wr:>7.1f}% {total:>+10.2f} {avg_pl:>+8.3f} {avg_e:>9.4f}")


# ─── Entry-price sweep for pre_order ─────────────────────────────────────────

def sweep_pre_order_price(windows: List[Window]) -> None:
    prices = [0.40, 0.42, 0.44, 0.45, 0.46, 0.47, 0.48, 0.49, 0.50]
    print("\n=== Pre-Order Entry Price Sweep (straddle both legs) ===")
    print(f"{'EntryP':>7} {'Trades':>7} {'Net/window':>11} {'TotalPL':>10}")
    for p in prices:
        trades = simulate_pre_order(windows, entry_price=p)
        wins   = len([r for r in trades if r.pl > 0])
        total  = sum(r.pl for r in trades)
        n_windows = len(trades) // 2 if trades else 0
        net_per   = total / n_windows if n_windows else 0
        print(f"{p:>7.2f} {len(trades):>7} {net_per:>+11.4f} {total:>+10.2f}")


# ─── Bucket analysis ──────────────────────────────────────────────────────────

def bucket_breakdown(trades: List[TradeResult], label: str) -> None:
    from collections import Counter
    buckets = Counter(r.bucket for r in trades)
    print(f"\n=== {label}: Win Rate by Bucket ===")
    print(f"{'Bucket':>22} {'Trades':>7} {'WinRate':>8} {'TotalPL':>10}")
    for bucket in sorted(buckets):
        subset = [r for r in trades if r.bucket == bucket]
        wins   = sum(1 for r in subset if r.pl > 0)
        total  = sum(r.pl for r in subset)
        wr     = wins / len(subset) * 100
        print(f"{bucket:>22} {len(subset):>7} {wr:>7.1f}% {total:>+10.2f}")


# ─── Time-of-day analysis ─────────────────────────────────────────────────────

def time_of_day_analysis(trades: List[TradeResult], label: str) -> None:
    """Group win rate by hour-of-day (UTC)."""
    from collections import defaultdict
    by_hour: Dict[int, List[TradeResult]] = defaultdict(list)
    for r in trades:
        try:
            # ts like "2026-03-07T20:53:56.780789+00:00"
            hour = int(r.trigger_candle_ts[11:13])
            by_hour[hour].append(r)
        except (ValueError, IndexError):
            pass
    if not by_hour:
        return
    print(f"\n=== {label}: Win Rate by Hour (UTC) ===")
    print(f"{'Hour':>5} {'Trades':>7} {'WinRate':>8} {'TotalPL':>10}")
    for h in sorted(by_hour):
        subset = by_hour[h]
        wins   = sum(1 for r in subset if r.pl > 0)
        total  = sum(r.pl for r in subset)
        wr     = wins / len(subset) * 100
        print(f"{h:>5}h {len(subset):>7} {wr:>7.1f}% {total:>+10.2f}")


# ─── Consecutive signal frequency analysis ────────────────────────────────────

def signal_frequency_analysis(windows: List[Window]) -> None:
    """
    For each window, count how many candles would fire trend_follow.
    This directly answers: "does trend_follow fire every minute or every 5 min?"
    The CSV is per-minute, so candle count == minutes where signal would fire.
    """
    print("\n=== Trend-Follow: Candles per Window Where Signal Would Fire ===")
    print("(Shows how many 1-minute snapshots within a 15-min window qualify.)")
    print("With a 5-min cron that's every ~3rd candle; expect 1-3 fires per window.)")
    from collections import Counter
    fire_counts: Counter = Counter()
    for w in windows:
        if w.winner is None:
            continue
        count = sum(
            1 for c in w.candles
            if c.trend_bucket == "Trend"
            and 0.50 <= c.elapsed_pct < 0.90
            and abs(c.prob_up - 0.5) >= 0.12
        )
        fire_counts[count] += 1

    total_windows = sum(fire_counts.values())
    triggered      = sum(v for k, v in fire_counts.items() if k > 0)
    print(f"\nTotal resolved windows:   {total_windows}")
    print(f"Windows with ≥1 qualifying candle: {triggered} ({100*triggered/total_windows:.1f}%)")
    print(f"\n{'Qualifying candles':>20} {'# Windows':>10} {'% of all windows':>18}")
    for count in sorted(fire_counts):
        pct = 100 * fire_counts[count] / total_windows
        print(f"{count:>20} {fire_counts[count]:>10} {pct:>17.1f}%")

    # If cron is every 5 min, max fires = ceil(6 min / 5 min) = 2
    # (50-90% of 15 min = 7.5-13.5 min = 6-min window, 2 cron fires expected)
    high = sum(v for k, v in fire_counts.items() if k > 2)
    if high:
        print(f"\n  WARNING: {high} windows have >2 qualifying candles.")
        print("  With a strict 5-min cron, at most 2 fires per window are expected.")
        print("  Multiple fires per window = the signal was re-firing every minute.")


# ─── Kelly criterion sizing ───────────────────────────────────────────────────

def kelly_analysis(trades: List[TradeResult], label: str) -> None:
    if not trades:
        return
    wins  = [r for r in trades if r.pl > 0]
    loses = [r for r in trades if r.pl <= 0]
    if not wins or not loses:
        return
    p = len(wins) / len(trades)
    avg_win  = sum(r.pl for r in wins)  / len(wins)
    avg_loss = abs(sum(r.pl for r in loses) / len(loses))
    b = avg_win / avg_loss           # win/loss ratio
    kelly_f = p - (1 - p) / b       # Kelly fraction of bankroll
    half_kelly = kelly_f / 2
    print(f"\n=== Kelly Criterion: {label} ===")
    print(f"  Win rate:      {p*100:.1f}%")
    print(f"  Avg win:       +${avg_win:.4f}")
    print(f"  Avg loss:      -${avg_loss:.4f}")
    print(f"  Win/loss ratio: {b:.3f}")
    print(f"  Full Kelly:    {kelly_f*100:+.1f}% of bankroll per trade")
    print(f"  Half Kelly:    {half_kelly*100:+.1f}% (recommended)")
    if kelly_f <= 0:
        print("  *** NEGATIVE EDGE — do not trade this strategy ***")


# ─── Cross-asset correlation ──────────────────────────────────────────────────

def cross_asset_correlation(all_windows: Dict[str, List[Window]]) -> None:
    """
    Check whether a trend-follow signal on BTC predicts the same direction
    on ETH/SOL/XRP within the same 15-min window.
    """
    print("\n=== Cross-Asset Momentum Correlation ===")
    print("When BTC fires trend_follow UP/DOWN, does ETH/SOL/XRP resolve the same way?")

    # Build lookup: market_start_ts → winner for each symbol
    winner_by_start: Dict[str, Dict[str, str]] = defaultdict(dict)
    for sym, windows in all_windows.items():
        for w in windows:
            if w.winner:
                winner_by_start[w.market_start_ts][sym] = w.winner

    btc_windows = all_windows.get("BTC", [])
    btc_signals = simulate_trend_follow(btc_windows, one_trade_per_window=True)

    if not btc_signals:
        print("  No BTC trend_follow signals found.")
        return

    for other in ["ETH", "SOL", "XRP"]:
        agree = total = 0
        for sig in btc_signals:
            # Find the market_start_ts from the candle
            btc_w = next(
                (w for w in btc_windows if w.condition_id == sig.condition_id),
                None,
            )
            if btc_w is None:
                continue
            other_winner = winner_by_start.get(btc_w.market_start_ts, {}).get(other)
            if other_winner is None:
                continue
            total += 1
            if sig.outcome == other_winner:
                agree += 1
        if total > 0:
            print(f"  BTC→{other}: {agree}/{total} agree ({100*agree/total:.1f}%) — "
                  + ("positive correlation" if agree/total > 0.55 else
                     "negative correlation" if agree/total < 0.45 else "no clear correlation"))


# ─── Trend persistence ────────────────────────────────────────────────────────

def trend_persistence(all_windows: Dict[str, List[Window]]) -> None:
    """
    If a window resolves UP/DOWN, does the NEXT window continue the same direction?
    This tests whether momentum carries over across 15-min windows.
    """
    print("\n=== Trend Persistence Across Windows ===")
    print("If window[i] resolves UP, does window[i+1] also resolve UP?")
    for sym, windows in all_windows.items():
        resolved = [w for w in windows if w.winner]
        if len(resolved) < 2:
            continue
        continuations = same = 0
        for i in range(len(resolved) - 1):
            a = resolved[i]
            b = resolved[i + 1]
            # Only count consecutive windows (not gaps)
            if a.market_end_ts != b.market_start_ts:
                continue
            continuations += 1
            if a.winner == b.winner:
                same += 1
        if continuations > 0:
            print(f"  {sym}: {same}/{continuations} consecutive windows continue direction "
                  f"({100*same/continuations:.1f}%)")


# ─── Pre-order bucket analysis ───────────────────────────────────────────────

def preorder_bucket_analysis(all_windows: Dict[str, List[Window]]) -> None:
    """
    Core question: should bucket gate pre_orders?

    For each market window we record:
      - The current window's bucket (vol+trend regime)
      - Whether the pre_order condition was met (67–95% elapsed candle exists)
      - The next window's outcome
      - Whether bucket persists into the next window (regime continuity)

    A pre_order straddle always nets +$0.4167 when BOTH legs fill.
    So the bucket question is really about FILL PROBABILITY and RISK:
      - High-vol markets have deeper books → both legs more likely to fill at 0.48
      - Low-vol markets have thinner books → one leg might miss → one-sided risk
    """
    print("\n" + "="*60)
    print("PRE-ORDER BUCKET ANALYSIS")
    print("="*60)

    by_sym_start: Dict[str, List[Window]] = {sym: ws for sym, ws in all_windows.items()}

    # ── 1. How often does the pre_order condition fire by bucket ──────────────
    print("\n── 1. Pre-order trigger frequency by current-window bucket ──")
    print("(Candle at 67–95% elapsed exists in window)")
    print(f"{'Bucket':>22} {'Windows':>8} {'Triggered':>10} {'Rate':>7}")

    from collections import defaultdict, Counter
    bucket_trigger: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "triggered": 0})
    for windows in by_sym_start.values():
        for w in windows:
            if w.winner is None:
                continue
            bucket = None
            triggered = False
            for c in w.candles:
                b = f"{c.vol_bucket}+{c.trend_bucket}"
                if bucket is None:
                    bucket = b
                if 0.667 <= c.elapsed_pct < 0.95:
                    triggered = True
                    bucket = b   # use bucket at trigger time
                    break
            if bucket is None:
                continue
            bucket_trigger[bucket]["total"] += 1
            if triggered:
                bucket_trigger[bucket]["triggered"] += 1

    for bucket in sorted(bucket_trigger):
        d = bucket_trigger[bucket]
        rate = d["triggered"] / d["total"] * 100 if d["total"] else 0
        print(f"{bucket:>22} {d['total']:>8} {d['triggered']:>10} {rate:>6.1f}%")

    # ── 2. Pre-order P&L by current-window bucket ─────────────────────────────
    print("\n── 2. Straddle P&L by current-window bucket (both legs, $5 each) ──")
    print(f"{'Bucket':>22} {'Straddles':>10} {'Net/straddle':>14} {'Total P&L':>11} {'Implied fill edge':>18}")
    print("  Note: assumes BOTH legs fill. Net/straddle = +$0.4167 is the theoretical max.")
    print("  Real-world shortfall vs $0.4167 indicates partial-fill risk.")

    bucket_pl: Dict[str, List[float]] = defaultdict(list)
    for windows in by_sym_start.values():
        sorted_ws = sorted(windows, key=lambda w: w.market_start_ts)
        for i, w in enumerate(sorted_ws[:-1]):
            if w.winner is None:
                continue
            # Find trigger bucket
            trigger_bucket = None
            for c in w.candles:
                if 0.667 <= c.elapsed_pct < 0.95:
                    trigger_bucket = f"{c.vol_bucket}+{c.trend_bucket}"
                    break
            if trigger_bucket is None:
                continue
            next_w = sorted_ws[i + 1]
            if next_w.winner is None:
                continue
            # Both legs at 0.48
            up_pl = 5 * (1 / 0.48 - 1) if next_w.winner == "UP" else -5.0
            dn_pl = 5 * (1 / 0.48 - 1) if next_w.winner == "DOWN" else -5.0
            net = up_pl + dn_pl
            bucket_pl[trigger_bucket].append(net)

    for bucket in sorted(bucket_pl):
        pl_list = bucket_pl[bucket]
        total = sum(pl_list)
        avg = total / len(pl_list)
        # All straddled windows should show +0.4167; deviations impossible in simulation
        # but useful to show the count available
        print(f"{bucket:>22} {len(pl_list):>10} {avg:>+14.4f} {total:>+11.4f} {'✓ full fill expected' if abs(avg - 0.4167) < 0.01 else '⚠ asymmetry':>18}")

    # ── 3. Bucket persistence: does current bucket predict next window's bucket ─
    print("\n── 3. Bucket persistence (current window bucket → next window bucket) ──")
    print("High persistence = regime is stable = pre_order fill conditions predictable")
    print(f"{'Current bucket':>22}  →  {'Next bucket':>22}  {'Count':>6}  {'%':>6}")

    bucket_transitions: Dict[str, Counter] = defaultdict(Counter)
    for windows in by_sym_start.values():
        sorted_ws = sorted(windows, key=lambda w: w.market_start_ts)
        for i, w in enumerate(sorted_ws[:-1]):
            next_w = sorted_ws[i + 1]
            if w.winner is None or next_w.winner is None:
                continue
            cur_b = next_b = None
            for c in w.candles:
                if 0.667 <= c.elapsed_pct < 0.95:
                    cur_b = f"{c.vol_bucket}+{c.trend_bucket}"
                    break
            for c in next_w.candles:
                next_b = f"{c.vol_bucket}+{c.trend_bucket}"
                break
            if cur_b and next_b:
                bucket_transitions[cur_b][next_b] += 1

    for cur_b in sorted(bucket_transitions):
        total = sum(bucket_transitions[cur_b].values())
        for next_b, count in bucket_transitions[cur_b].most_common():
            pct = count / total * 100
            marker = " ← same" if cur_b == next_b else ""
            print(f"{cur_b:>22}  →  {next_b:>22}  {count:>6}  {pct:>5.1f}%{marker}")
        print()

    # ── 4. Timing gate analysis: when does the cron hit the pre_order window ───
    print("\n── 4. Cron timing alignment with pre_order window ──")
    print("Pre_order fires at 67–95% elapsed = 603s–855s into a 900s window.")
    print("Executor gate: only execute if T-until-next <= 300s = elapsed >= 66.7%.")
    print("With 5-min (300s) cron, the gate aligns ~once per 15-min window.")
    print()

    # Count how many candles per window are in the 67-95% zone
    zone_counts = Counter()
    for windows in by_sym_start.values():
        for w in windows:
            if w.winner is None:
                continue
            n = sum(1 for c in w.candles if 0.667 <= c.elapsed_pct < 0.95)
            zone_counts[n] += 1

    total_w = sum(zone_counts.values())
    print(f"  Total resolved windows: {total_w}")
    print(f"  {'Zone candles':>13} {'Windows':>8} {'%':>7}  Note")
    for k in sorted(zone_counts):
        pct = zone_counts[k] / total_w * 100
        note = ""
        if k == 0:
            note = "← no pre_order fires (cron miss)"
        elif k <= 2:
            note = "← typical (5-min cron)"
        else:
            note = "← cron ran more frequently here"
        print(f"  {k:>13} {zone_counts[k]:>8} {pct:>6.1f}%  {note}")

    cron_miss = zone_counts[0]
    miss_rate = cron_miss / total_w * 100
    print(f"\n  Cron-miss rate: {miss_rate:.1f}% of windows have NO qualifying candle")
    print(f"  → These windows get 0 pre_order signals even though the market opened")

    # ── 5. Bucket recommendation ──────────────────────────────────────────────
    print("\n── 5. Bucket gate recommendation ──")
    print("""
  Mathematical edge from pre_order straddle is ALWAYS +$0.4167/window (when both legs fill).
  Bucket gating does NOT change the per-fill P&L — it changes FILL PROBABILITY.

  Key question: in which buckets does the 0.48 limit order reliably fill on BOTH sides?

  Hypothesis:
    HighVol+Trend   → Deep order book, active market. Both legs likely fill. ✓ TRADE
    HighVol+Range   → Volatile but choppy. Both legs likely fill (high volume). ✓ TRADE
    LowVol+Trend    → Thin book, quiet market. Risk: one leg might not fill. ⚠ CAUTION
    LowVol+Range    → Very thin book, choppy. High partial-fill risk. ✗ SKIP

  Recommended filter: only pre_order when vol_bucket == "HighVol"
  This trades ~50% of windows but with much higher confidence of full straddle fill.

  Alternatively: use bucket to SIZE the pre_order bet:
    HighVol → $10 per leg (confident fill)
    LowVol  → $5 per leg or skip (thin book)
  """)


# ─── Summary table ────────────────────────────────────────────────────────────

def print_summary(label: str, trades: List[TradeResult]) -> None:
    if not trades:
        print(f"\n{label}: no trades")
        return
    wins   = sum(1 for r in trades if r.pl > 0)
    total  = sum(r.pl for r in trades)
    avg_e  = sum(r.entry_price for r in trades) / len(trades)
    avg_pl = total / len(trades)
    wr     = wins / len(trades) * 100
    print(f"\n{'='*60}")
    print(f" Strategy : {label}")
    print(f" Trades   : {len(trades)}")
    print(f" Wins     : {wins}  ({wr:.1f}%)")
    print(f" Total P&L: {total:+.4f} USDC")
    print(f" Avg P&L  : {avg_pl:+.4f} USDC")
    print(f" Avg entry: {avg_e:.4f}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol",   default="all",  help="BTC|ETH|SOL|XRP|all")
    ap.add_argument("--strategy", default="all",  help="trend|directional|pre_order|all")
    ap.add_argument("--size",     type=float, default=5.0, help="Bet size USDC")
    ap.add_argument("--sweep",    action="store_true", help="Run threshold sweeps")
    ap.add_argument("--freq",     action="store_true", help="Signal frequency analysis")
    ap.add_argument("--preorder", action="store_true", help="Run pre-order bucket deep-dive")
    args = ap.parse_args()

    syms = SYMBOLS if args.symbol == "all" else [args.symbol.upper()]
    all_windows: Dict[str, List[Window]] = {}
    for sym in syms:
        print(f"Loading {sym}...", end=" ", flush=True)
        ws = load_symbol(sym)
        resolved = [w for w in ws if w.winner]
        print(f"{len(ws)} windows, {len(resolved)} resolved")
        all_windows[sym] = ws

    all_trend   : List[TradeResult] = []
    all_dir     : List[TradeResult] = []
    all_pre     : List[TradeResult] = []

    for sym, windows in all_windows.items():
        all_trend += simulate_trend_follow(windows, size=args.size)
        all_dir   += simulate_directional(windows, size=args.size)
        all_pre   += simulate_pre_order(windows, size=args.size)

    print_summary("trend_follow (1-trade-per-window)", all_trend)
    print_summary("directional_90pct",                 all_dir)
    print_summary("pre_order straddle",                all_pre)

    bucket_breakdown(all_trend, "trend_follow")
    bucket_breakdown(all_dir,   "directional_90pct")

    time_of_day_analysis(all_trend, "trend_follow")
    time_of_day_analysis(all_dir,   "directional_90pct")

    kelly_analysis(all_trend, "trend_follow")
    kelly_analysis(all_dir,   "directional_90pct")

    if args.sweep:
        combined_windows = [w for ws in all_windows.values() for w in ws]
        sweep_trend_follow_threshold(combined_windows)
        sweep_pre_order_price(combined_windows)

    if args.freq:
        combined_windows = [w for ws in all_windows.values() for w in ws]
        signal_frequency_analysis(combined_windows)

    if args.preorder:
        preorder_bucket_analysis(all_windows)

    cross_asset_correlation(all_windows)
    trend_persistence(all_windows)

    print("\n" + "="*60)
    print("COMBINED TOTALS")
    combined = all_trend + all_dir + all_pre
    print(f"  All strategies P&L: {sum(r.pl for r in combined):+.4f} USDC")
    print(f"  trend_follow  P&L:  {sum(r.pl for r in all_trend):+.4f} USDC")
    print(f"  directional   P&L:  {sum(r.pl for r in all_dir):+.4f} USDC")
    print(f"  pre_order     P&L:  {sum(r.pl for r in all_pre):+.4f} USDC")


if __name__ == "__main__":
    main()
