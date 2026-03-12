# Indicators — Complete Reference

> Bob the Builder — Polymarket 15-Minute Crypto Trading Bot
> Markets: BTC, ETH, SOL, XRP · Window: 15 minutes · Stake: $5 USDC fixed

---

## Overview

The bot uses **8 trigger types** (indicators/signals) to decide when and how to enter trades on Polymarket 15-minute UP/DOWN crypto markets. Each 15-minute window is a binary market: BTC (or ETH/SOL/XRP) ends **UP** or **DOWN** relative to its opening price.

Every signal produces:
- `symbol` — which asset (BTC, ETH, SOL, XRP)
- `outcome` — UP or DOWN
- `side` — always BUY (buying the binary outcome token)
- `size` — stake in USDC (fixed $5)
- `price` — limit price (0.01–1.00)
- `confidence` — 0.0–1.0, derived from P(UP)
- `trigger` — which indicator fired
- `reason` — full reasoning string logged to `signals.json`

---

## Core Inputs

Before any indicator fires, the bot calculates these market inputs each polling cycle (~60 seconds):

### P(UP) — Probability of UP Outcome

Derived from the live Polymarket order book mid-prices.

```
P(UP) = up_mid_price
```

Where `up_mid_price = (bestBid + bestAsk) / 2` for the UP token.
Since UP + DOWN = 1.00 in a binary market, `P(DOWN) = 1 - P(UP)`.

**Example from `markets.json`:**
```json
"BTC": { "up_price": 0.36, "down_price": 0.64 }
→ P(UP) = 0.36, P(DOWN) = 0.64
```

---

### RV60 — Realized Volatility (60-minute window)

Measures how much price has moved over the last 60 minutes using log returns.

```
log_return = ln(price_t / price_t-1)
RV60       = annualized std dev of log returns over 60m
```

Stored per market in the `calculations` table as `realized_vol_60m`.

**Interpretation:**
- `RV60 > ~8` → `HighVol` bucket
- `RV60 ≤ ~8` → `LowVol` bucket

**Live examples:**
| Symbol | RV60 | Vol Bucket |
|--------|------|-----------|
| BTC | 9.86 | HighVol |
| ETH | 6.80 | LowVol |
| SOL | 14.95 | HighVol |
| XRP | 10.58 | HighVol |

---

### Eff60 — Efficiency Ratio (60-minute window)

Measures trend strength: the ratio of net displacement to total path traveled.

```
Eff60 = |net price move over 60m| / sum(|all 1m moves| over 60m)
```

Range: 0.0–1.0
- High Eff60 (≥ ~0.020) → price moved directionally → `Trend` bucket
- Low Eff60 (< ~0.020) → price moved choppily → `Range` bucket

Stored as `efficiency_60m` in the `calculations` table.

**Live examples:**
| Symbol | Eff60 | Trend Bucket |
|--------|-------|-------------|
| BTC | 0.0683 | Trend |
| ETH | 0.0187 | Trend |
| SOL | 0.0570 | Trend |
| XRP | 0.0066 | Range |

---

### Market Bucket (4-Way Classification)

Every market is assigned one of 4 buckets each cycle:

| Bucket | Vol | Trend | Character |
|--------|-----|-------|-----------|
| `HighVol+Trend` | High | Trending | Strong directional moves |
| `HighVol+Range` | High | Range-bound | Volatile but choppy |
| `LowVol+Trend` | Low | Trending | Quiet but directional |
| `LowVol+Range` | Low | Range-bound | Quiet and choppy |

This bucket is stamped on every signal and used to slice performance reporting.

---

### Elapsed % — How Far Through the 15-Minute Window

```
elapsed_pct = (current_time - window_open_time) / 900 seconds
```

Most directional triggers only fire after a certain elapsed threshold, because markets are more informative later in the window.

**Live example:**
```json
"BTC": { "elapsed_pct": 0.1393, "remaining_sec": 774.7 }
→ 13.9% elapsed, ~12.9 minutes remaining
```

---

### Directional Probability Projections (dir_60pct / dir_80pct / dir_90pct)

These are model-estimated P(UP) values at future time checkpoints, derived from the current price and volatility.

```
dir_60pct = projected P(UP) if evaluated at 60% of market elapsed
dir_80pct = projected P(UP) if evaluated at 80% of market elapsed
dir_90pct = projected P(UP) if evaluated at 90% of market elapsed
```

**Live example:**
```json
"BTC": { "dir_60pct": 0.3768, "dir_80pct": 0.3614, "dir_90pct": 0.3537 }
```

---

### Prob Metrics (prob_008 / prob_012 / prob_020)

Alternative probability estimates using different drift assumptions:

| Field | Drift Assumption |
|-------|-----------------|
| `prob_008` | 0.8% drift |
| `prob_012` | 1.2% drift |
| `prob_020` | 2.0% drift |

Used for backtesting and analysis. Not directly used in live trigger logic.

---

## Edge Calculation

Used by directional triggers to size confidence and price:

```
edge = |P(UP) - 0.5| × 2
```

- `P(UP) = 0.87` → `edge = |0.87 - 0.5| × 2 = 0.74`
- `P(UP) = 0.50` → `edge = 0`
- `P(UP) = 0.00` → `edge = 1.0` (maximum)

Adaptive edge floor: `0.050` — the minimum edge required to trigger.

---

## The 8 Indicators

---

### 1. `pre_open` — Pre-Open Limit Order

**Category:** Timing
**Concept:** Place limit orders on both sides of a market before it officially opens, buying below fair value.

**Logic:**
1. Detect market that is 329–330 seconds (~5.5 min) before its open time
2. Place a BUY of UP at `0.48`
3. Place a BUY of DOWN at `0.48`

Since the fair value of each side is ~0.50 at open, buying at 0.48 provides a theoretical 2-cent edge on each side.

**Configuration:**
| Parameter | Value |
|-----------|-------|
| Entry timing | 329–330 seconds before market open |
| Entry price | 0.48 |
| Stake | $5 USDC |
| Outcome | Both UP and DOWN |
| Order type | Limit order |

**Example signal reason:**
```
PRE-OPEN limit order — 329s before market start.
Buying UP at 0.48 (below ~0.50 fair value)
```

**Performance (all-time):**
| Total | UP Trades | DOWN Trades | Win Rate |
|-------|-----------|-------------|----------|
| 648 | 324 | 324 | 50.0% |

**By bucket:**
| Bucket | Win Rate |
|--------|---------|
| HighVol+Trend | 50.0% |
| HighVol+Range | 50.0% |
| LowVol+Trend | 50.0% |
| LowVol+Range | 50.0% |

**Notes:** Perfectly symmetric 50% win rate reflects the symmetric entry on both sides. This is a volume/liquidity play, not a directional edge. Profitability depends entirely on achieving the 0.48 fill (getting better than fair value).

---

### 2. `directional_60pct` — Directional at 60% Elapsed

**Category:** Directional / Time Threshold
**Concept:** At the 60% mark of the 15-minute window (9 minutes in), if the market price has moved significantly enough to indicate direction, bet on that direction.

**Logic:**
1. Wait until `elapsed_pct ≥ 0.60`
2. Compute `P(UP)` from current order book prices
3. If `P(UP) ≥ 0.6` → bet UP
4. If `P(UP) ≤ 0.4` → bet DOWN
5. Calculate `edge = |P(UP) - 0.5| × 2`
6. `confidence = P(UP)` (or 1.0 if extreme)
7. Fire only if `edge ≥ adaptive_edge_floor (0.050)`

**Configuration:**
| Parameter | Value |
|-----------|-------|
| Trigger time | ≥ 60% elapsed (~9 min into 15-min window) |
| P(UP) threshold | ≥ 0.60 for UP, ≤ 0.40 for DOWN |
| Adaptive edge floor | 0.050 |
| Stake | $5 USDC |
| Fires once per window | Yes (suppressed after first signal) |

**Performance:**
| Total | WIN | LOSS | Win Rate |
|-------|-----|------|----------|
| 2 | 0 | 2 | 0.0% |

**Notes:** Very small sample size (only 2 trades). Firing too early — at 60% elapsed there is insufficient market information. This trigger has been largely disabled in favour of higher elapsed thresholds.

---

### 3. `directional_80pct` — Directional at 80% Elapsed

**Category:** Directional / Time Threshold
**Concept:** Same as `directional_60pct` but fires at 80% of the window elapsed (12 minutes in).

**Logic:**
1. Wait until `elapsed_pct ≥ 0.80`
2. Compute `P(UP)` from current order book prices
3. If `P(UP) ≥ 0.6` → bet UP
4. If `P(UP) ≤ 0.4` → bet DOWN
5. Calculate `edge = |P(UP) - 0.5| × 2`
6. `confidence = P(UP)` (or 1.0 if extreme)
7. Fire only if `edge ≥ 0.050`

**Configuration:**
| Parameter | Value |
|-----------|-------|
| Trigger time | ≥ 80% elapsed (~12 min into 15-min window) |
| P(UP) threshold | ≥ 0.60 for UP, ≤ 0.40 for DOWN |
| Adaptive edge floor | 0.050 |
| Stake | $5 USDC |
| Fires once per window | Yes |

**Performance:**
| Total | WIN | LOSS | Win Rate |
|-------|-----|------|----------|
| 15 | 2 | 13 | 13.3% |

**By bucket:**
| Bucket | Win Rate |
|--------|---------|
| HighVol+Trend | 0.0% (2 trades) |
| HighVol+Range | 0.0% (5 trades) |
| LowVol+Trend | 0.0% (1 trade) |
| LowVol+Range | 0.0% (1 trade) |

**Notes:** Poor performance across all bucket types. The 80% time point does not appear to be predictive. All trades have been DOWN signals — there is likely a systematic bias in conditions that triggered it. Needs investigation before use.

---

### 4. `directional_90pct` — Directional at 90% Elapsed

**Category:** Directional / Time Threshold
**Concept:** At 90% elapsed (13.5 min in), the market price is the strongest predictor of final outcome. Bet on the direction the market is already pricing.

**Logic:**
1. Wait until `elapsed_pct ≥ 0.90`
2. Compute `P(UP)` from current order book prices
3. If `P(UP) ≥ 0.6` → bet UP
4. If `P(UP) ≤ 0.4` → bet DOWN
5. `edge = |P(UP) - 0.5| × 2`
6. `confidence = P(UP)` (or `1.0` if `P(UP) ≥ 0.999` or `≤ 0.001`)
7. Fire only if `edge ≥ 0.050`

**Entry price logic:**
- If `confidence = 1.0` (market at extreme): price = `0.99` or `1.00`
- Otherwise: price derived from `P(UP)` proximity to 0.5

**Configuration:**
| Parameter | Value |
|-----------|-------|
| Trigger time | ≥ 90% elapsed (~13.5 min into 15-min window) |
| P(UP) threshold | ≥ 0.60 for UP, ≤ 0.40 for DOWN |
| Adaptive edge floor | 0.050 |
| Stake | $5 USDC |
| Fires once per window | Yes |

**Example signal reason:**
```
DIRECTIONAL at 90pct (92% elapsed) — P(UP)=0.872 gives edge=0.372 toward UP.
Bucket=HighVol+Range (RV60=13.69274, Eff60=0.001).
Fixed $5 USDC stake @ conf=0.872. [adaptive edge=0.050]
```

**Performance:**
| Total | WIN | LOSS | Win Rate |
|-------|-----|------|----------|
| 21 | 13 | 8 | 61.9% |

**By bucket:**
| Bucket | Trades | Win Rate |
|--------|--------|---------|
| HighVol+Trend | 1 | 100.0% |
| HighVol+Range | 8 | 100.0% |
| unknown | 12 | 33.3% |

**Notes:** This is the current primary trigger in V2/V3 dev strategy. The "unknown" bucket (missing market classification) reduces overall win rate. When bucket data is available, performance is exceptional (100%). The late-window timing means price is a very strong signal — markets near resolution price themselves close to the true final value.

---

### 5. `trend_follow` — Trend-Following Signal

**Category:** Technical Analysis
**Concept:** When a market is in a `HighVol+Trend` condition (trending with conviction), follow the direction of the trend.

**Logic:**
1. Check market bucket: only fires when `vol_bucket = HighVol` AND `trend_bucket = Trend`
2. Use RV60 and Eff60 to confirm directional conviction
3. Use P(UP) to determine direction (UP if P(UP) > 0.5, DOWN if P(UP) < 0.5)
4. Generate signal with conviction scaled to Eff60 magnitude

**Configuration:**
| Parameter | Value |
|-----------|-------|
| Required bucket | HighVol+Trend |
| RV60 requirement | HighVol (> threshold) |
| Eff60 requirement | Trend (≥ ~0.020) |
| Stake | $5 USDC |
| Fires once per window | Yes |

**Example signal reason (from `data_collector.md`):**
```
Bucket: HighVol+Trend, RV60=13.43025, Eff60=0.023 → Signal generated (UP)
```

**Performance:**
| Total | WIN | LOSS | Win Rate |
|-------|-----|------|----------|
| 162 | 121 | 41 | 74.7% |

**By bucket:**
| Bucket | Trades | Win Rate |
|--------|--------|---------|
| HighVol+Trend | 99 | 80.8% |
| unknown | 63 | 65.1% |

**Notes:** This is the **best-performing indicator** in the system. The 80.8% win rate in classified HighVol+Trend buckets is exceptional. The key insight: when volatility is high AND price is trending efficiently, the market is resolving toward its final direction with conviction. This trigger only fires in its target condition, so trade count is limited.

---

### 6. `forced_edge` — Forced Trade with Probabilistic Edge

**Category:** Fallback / Safety
**Concept:** If no signal has been placed by 60% of the window, force a trade using the current P(UP) as a directional guide.

**Logic:**
1. Check: `elapsed_pct ≥ 0.60` AND no signal placed this window yet
2. Use P(UP) to determine direction:
   - `P(UP) > 0.5` → bet UP
   - `P(UP) < 0.5` → bet DOWN
3. Entry price: `0.99` for the chosen side (high-confidence entry)
4. `edge = |P(UP) - 0.5| × 2`
5. Stake: `$5 USDC`

**Configuration:**
| Parameter | Value |
|-----------|-------|
| Trigger condition | elapsed_pct ≥ 0.60 AND no prior signal |
| Trigger time | ~9 minutes in |
| P(UP) direction split | 0.5 (50%) |
| Entry price | 0.99 |
| Stake | $5 USDC |
| Can be suppressed | Yes (disabled in V2 strategy) |

**Example signal reason:**
```
FORCED TRADE at 60% elapsed — no prior signal for this window.
P(UP)=0.095, choosing DOWN. Fixed $5 USDC stake
```

**Performance:**
| Total | WIN | LOSS | Win Rate |
|-------|-----|------|----------|
| 247 | 135 | 112 | 54.7% |

**By bucket:**
| Bucket | Trades | Win Rate |
|--------|--------|---------|
| HighVol+Trend | 120 | 65.0% |
| HighVol+Range | 11 | 18.2% |
| LowVol+Trend | 53 | 45.3% |
| LowVol+Range | 63 | 49.2% |

**Notes:** Strong in HighVol+Trend (65%), poor in HighVol+Range (18.2%). The forced nature of this trigger means it fires in markets where better triggers (trend_follow, directional) failed to fire. Edge is modest at best. In V2 development strategy, this trigger is suppressed to avoid low-edge trades.

---

### 7. `forced_coin` — Forced Trade with Coin Flip

**Category:** Fallback / Safety
**Concept:** Same as `forced_edge` but treats the decision as near-random when P(UP) is close to 0.5.

**Logic:**
1. Check: `elapsed_pct ≥ 0.60` AND no signal placed this window yet
2. P(UP) is near 0.5 (market is undecided)
3. Choose UP (or randomized)
4. Entry price: `0.83`–`0.99` (adaptive based on P(UP) proximity to 0.5)
5. Stake: `$5 USDC`

**Configuration:**
| Parameter | Value |
|-----------|-------|
| Trigger condition | elapsed_pct ≥ 0.60 AND no prior signal |
| P(UP) condition | ≈ 0.50 (near-random market) |
| Entry price | 0.83–0.99 |
| Stake | $5 USDC |
| Can be suppressed | Yes |

**Example signal reason:**
```
FORCED TRADE at 60% elapsed — no prior signal for this window.
P(UP)=0.500, choosing UP. Fixed $5 USDC stake
```

**Performance:**
| Total | WIN | LOSS | Win Rate |
|-------|-----|------|----------|
| 75 | 40 | 35 | 53.3% |

**By bucket:**
| Bucket | Trades | Win Rate |
|--------|--------|---------|
| HighVol+Trend | 3 | 33.3% |
| HighVol+Range | 31 | 45.2% |
| LowVol+Trend | 16 | 62.5% |
| LowVol+Range | 25 | 60.0% |

**Notes:** Performs better in low-vol environments. The coin-flip nature means this is essentially random entry — the modest positive win rate may be noise. Best used as a "keep the bot active" mechanism, not as a true edge. Should be suppressed in high-conviction strategy configurations.

---

### 8. `arb` — Arbitrage

**Category:** Arbitrage
**Concept:** Detect mispricing where UP + DOWN prices sum to less than 1.0, allowing a risk-free profit by buying both sides.

**Logic:**
1. For each market, calculate: `arb = 1.0 - (up_price + down_price)`
2. If `arb > 0` (sum < 1.0), both sides can be bought simultaneously for guaranteed profit
3. Buy both UP and DOWN at their current prices
4. Profit = `arb` per dollar staked on each side

**Configuration:**
| Parameter | Value |
|-----------|-------|
| Trigger condition | up_price + down_price < 1.00 |
| Entry | Buy both UP and DOWN simultaneously |
| Stake | $5 USDC per side |
| Arb field in markets.json | `"arb": null` (inactive when no arb exists) |

**Live example from `markets.json`:**
```json
"BTC": { "up_price": 0.36, "down_price": 0.64, "arb": null }
→ 0.36 + 0.64 = 1.00 → no arb
```

**Performance:**
| Total | WIN | LOSS | Win Rate |
|-------|-----|------|----------|
| 0 | — | — | N/A |

**Notes:** Currently inactive — the bot monitors for arb but has found no exploitable opportunities. In practice, Polymarket maintains efficient pricing that eliminates arb very quickly. The `arb` field in `markets.json` will show a non-null value when an opportunity is detected.

---

## Signal Priority & Suppression

Indicators fire in a defined priority order within each 15-minute window. Once a signal is placed, subsequent lower-priority triggers are suppressed for that window.

**Priority order (highest to lowest):**
1. `pre_open` — fires before window opens
2. `trend_follow` — fires early if bucket = HighVol+Trend
3. `directional_90pct` — fires at 90% elapsed
4. `directional_80pct` — fires at 80% elapsed (if 90pct not yet fired)
5. `directional_60pct` — fires at 60% elapsed (if nothing yet)
6. `arb` — fires whenever arb detected
7. `forced_edge` — fallback at 60% if nothing fired (can be suppressed)
8. `forced_coin` — fallback at 60% if nothing fired (can be suppressed)

---

## Strategy Versions

Three configuration versions exist, varying which triggers are active:

| Version | Status | Active Triggers | Suppressed |
|---------|--------|-----------------|-----------|
| V1.0 Prod | Production | pre_open, forced_edge, forced_coin | trend_follow, directional_* |
| V2.0 Dev | Development | trend_follow, directional_90pct, pre_open | forced_edge, forced_coin |
| V3.0 Dev | Development | All triggers | None |

---

## Performance Comparison Table

| Trigger | Trades | Win Rate | Best Bucket | Notes |
|---------|--------|----------|-------------|-------|
| `trend_follow` | 162 | **74.7%** | HighVol+Trend (80.8%) | Best performer |
| `directional_90pct` | 21 | **61.9%** | HighVol+Range (100%) | Strong when classified |
| `forced_edge` | 247 | **54.7%** | HighVol+Trend (65%) | Bucket-sensitive |
| `forced_coin` | 75 | **53.3%** | LowVol+Trend (62.5%) | Near-random |
| `pre_open` | 648 | **50.0%** | All (50%) | Volume/liquidity play |
| `directional_80pct` | 15 | **13.3%** | — | Underperforming |
| `directional_60pct` | 2 | **0.0%** | — | Too early, inactive |
| `arb` | 0 | **N/A** | — | No opportunities found |
| **TOTAL** | **1,350** | **53.4%** | | |

---

## Database Schema Reference

**`market_data` table** — raw tick data collected every 60 seconds:
```sql
id, timestamp, market_id, market_name, token_id, outcome,
price, bid, ask, spread, volume_24h, bid_depth, ask_depth,
last_price, last_trade_size, last_trade_side, last_trade_asset,
created_at
```

**`calculations` table** — derived indicators per market per cycle:
```sql
id, timestamp, market_id, interval_minutes,
log_return, realized_vol_60m, efficiency_60m,
vol_bucket, trend_bucket, created_at
```

---

## Replication Guide

To replicate the indicator system from scratch:

### Step 1 — Data Collection
- Poll `https://polymarket.com/crypto/15M` to get active market slugs
- For each slug, fetch from `https://gamma-api.polymarket.com/markets?slug={slug}`
- For each token, fetch order book from `https://clob.polymarket.com/book?token_id={tokenId}`
- Compute mid-price: `mid = (bestBid + bestAsk) / 2`
- Store to `market_data` table every 60 seconds

### Step 2 — Calculate Indicators
Using rolling 60-minute window of stored prices per `market_id`:
```js
// Log return per tick
log_return = Math.log(price_t / price_t_minus_1)

// Realized Volatility (RV60)
realized_vol_60m = stdDev(log_returns_60m) * Math.sqrt(annualization_factor)

// Efficiency Ratio (Eff60)
net_move = Math.abs(price_now - price_60m_ago)
total_path = sum(Math.abs(price_t - price_t_minus_1)) over 60m
efficiency_60m = net_move / total_path   // 0.0 to 1.0

// Bucket classification
vol_bucket   = realized_vol_60m > VOL_THRESHOLD ? "HighVol" : "LowVol"
trend_bucket = efficiency_60m >= EFF_THRESHOLD  ? "Trend"   : "Range"
```

### Step 3 — P(UP) and Elapsed %
```js
P_UP       = up_mid_price                    // from order book
elapsed    = (Date.now()/1000 - window_open) / 900   // 0.0 to 1.0
edge       = Math.abs(P_UP - 0.5) * 2
confidence = (P_UP >= 0.999 || P_UP <= 0.001) ? 1.0 : P_UP
```

### Step 4 — Trigger Evaluation (per market, each cycle)
```
IF pre_open condition → fire pre_open
ELSE IF HighVol+Trend AND no signal yet → fire trend_follow
ELSE IF elapsed >= 0.90 AND P_UP edge sufficient → fire directional_90pct
ELSE IF elapsed >= 0.80 AND P_UP edge sufficient → fire directional_80pct
ELSE IF elapsed >= 0.60 AND P_UP edge sufficient → fire directional_60pct
ELSE IF arb detected → fire arb
ELSE IF elapsed >= 0.60 AND no signal yet:
  IF P_UP is near 0.5 → fire forced_coin
  ELSE → fire forced_edge
```

### Step 5 — Signal Output
Write to `signals.json`:
```json
{
  "symbol": "BTC",
  "slug": "btc-updown-15m",
  "outcome": "UP",
  "side": "BUY",
  "size": 5.0,
  "price": 0.99,
  "confidence": 0.872,
  "trigger": "directional_90pct",
  "reason": "DIRECTIONAL at 90pct (93% elapsed) — P(UP)=0.872 gives edge=0.372 toward UP. Bucket=HighVol+Range (RV60=14.96254, Eff60=0.009). Fixed $5 USDC stake @ conf=0.872. [adaptive edge=0.050]"
}
```

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `market-data-collector.js` | Data collection loop (polls every 60s) |
| `data_exports/markets.json` | Live market state — all indicators per symbol |
| `data_exports/signals.json` | Latest active signals with trigger + reasoning |
| `data_exports/calculations_recent.csv` | Historical RV60/Eff60/bucket data |
| `data_exports/BTC.csv` / `ETH.csv` / `SOL.csv` / `XRP.csv` | Per-symbol historical data |
| `reports/trigger_summary.md` | Performance by trigger × bucket |
| `reports/decision_summary.md` | Last 48h signal log (200 signals) |
| `reports/decision_tracker.md` | Full trade history with P&L |
| `reports/market_pnl.md` | P&L by market |

---

_Auto-generated documentation — Bob the Builder_
