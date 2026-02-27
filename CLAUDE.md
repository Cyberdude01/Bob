# CLAUDE.md — AI Assistant Guide for Bob (Polymarket Data Collector)

## Project Overview

**Bob** is a Node.js bot that monitors 15-minute cryptocurrency prediction markets on [Polymarket](https://polymarket.com). It collects real-time market data (prices, order book depth, last trades), stores it in a local SQLite database, calculates technical metrics (realized volatility, log returns, efficiency scores), and exports data to CSV for analysis.

---

## Repository Structure

```
/
├── market-data-collector.js   # Main source file — all logic lives here
├── package.json               # npm metadata and dependencies
├── README.md                  # Minimal project header
├── market-data.db             # SQLite database (created at runtime, not committed)
└── data_exports/              # CSV exports for offline analysis
    ├── btc_eth_up_recent.csv
    ├── btc_eth_up_with_outcome_prices.csv
    ├── calculations_recent.csv
    ├── market_data_25Feb.csv
    ├── market_data_filtered_export.csv
    └── market_data_recent.csv
```

The project is intentionally minimal — a single main file with no build step.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Node.js |
| Database | SQLite3 (local file `./market-data.db`) |
| HTTP | axios |
| Env config | dotenv |
| Ethereum/Wallet | ethers.js v5 |
| Polymarket SDK | @polymarket/clob-client, @polymarket/order-utils, @polymarket/relayer-client |

---

## Getting Started

### Prerequisites
- Node.js (any recent LTS)
- npm

### Installation
```bash
npm install
```

### Environment Variables
The project uses `dotenv`. Create a `.env` file in the project root. No `.env.example` is currently committed, but relevant variables would include any API keys or wallet credentials needed by the Polymarket SDK clients.

### Running the Collector
```bash
node market-data-collector.js
```
The script starts immediately, collects data, then repeats every **60 seconds**.

---

## Architecture — market-data-collector.js

This is the single entry point and contains all logic. Key sections:

### Database Initialization
- Creates (or migrates) the `market_data` and `calculations` tables on startup.
- Uses a **manual migration pattern**: new columns are added with `ALTER TABLE` statements wrapped in try/catch — errors are silently ignored so existing databases aren't broken.
- Current schema is **v4**.

### Core Functions

| Function | Purpose |
|---|---|
| `getActive15MinSlugs()` | Scrapes `polymarket.com/crypto/15M` for active market slugs |
| `fetchMarketBySlug(slug)` | Fetches market metadata from `gamma-api.polymarket.com` |
| `fetchOrderBook(tokenId)` | Fetches order book snapshot from `clob.polymarket.com` |
| `fetchLastTradeForMarket(conditionId)` | Fetches the most recent trade from `data-api.polymarket.com` |
| `collectData()` | Main orchestration loop: calls all of the above, writes to DB |

### Collection Loop
```
startup → collectData() → setInterval(collectData, 60_000)
```
- Rate-limiting: 200ms delay between per-market API calls (`await new Promise(r => setTimeout(r, 200))`)

### External APIs Used

| API | URL Pattern | Purpose |
|---|---|---|
| Polymarket website | `polymarket.com/crypto/15M` | Scrape active market slugs |
| Gamma API | `gamma-api.polymarket.com/markets?slug={slug}` | Market metadata |
| CLOB API | `clob.polymarket.com/book?token_id={tokenId}` | Order book |
| Data API | `data-api.polymarket.com/trades?market={conditionId}&limit=1` | Last trade |

---

## Database Schema

### `market_data` table

| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| timestamp | INTEGER | Unix timestamp of collection |
| market_id | TEXT | Condition ID (from Polymarket) |
| market_name | TEXT | Human-readable question text |
| token_id | TEXT | CLOB token ID |
| outcome | TEXT | e.g., "Yes" / "No" |
| price | REAL | Mid price |
| bid | REAL | Best bid |
| ask | REAL | Best ask |
| spread | REAL | Ask − Bid |
| volume_24h | REAL | 24-hour volume |
| bid_depth | REAL | Sum of top 5 bid quantities |
| ask_depth | REAL | Sum of top 5 ask quantities |
| last_price | REAL | Last trade price |
| last_trade_size | REAL | Last trade size |
| last_trade_side | TEXT | "BUY" or "SELL" |
| last_trade_asset | TEXT | Asset token ID from last trade |
| created_at | INTEGER | Unix timestamp of DB insert |

**Index:** `idx_market_timestamp (market_id, timestamp)`

### `calculations` table

| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| timestamp | INTEGER | Unix timestamp |
| market_id | TEXT | Condition ID |
| interval_minutes | INTEGER | Lookback interval |
| log_return | REAL | Log price return |
| realized_vol_60m | REAL | Realized volatility over 60 min |
| efficiency_60m | REAL | Market efficiency score over 60 min |
| vol_bucket | TEXT | Volatility category label |
| trend_bucket | TEXT | Trend category label |
| created_at | INTEGER | Unix timestamp of DB insert |

---

## Development Conventions

### Code Style
- **No linter or formatter is configured.** Follow the existing style in `market-data-collector.js`:
  - 2-space indentation
  - `async/await` for all async operations (no raw Promise chains)
  - `console.log` for runtime output; no structured logging library
  - Single-file design — keep new logic in `market-data-collector.js` unless the file grows unwieldy

### Database Migrations
When adding a new column:
1. Add the column to the `CREATE TABLE` statement (for fresh installs).
2. Also add an `ALTER TABLE ... ADD COLUMN` statement in the migration block, wrapped in try/catch (for existing installs).
3. Increment the schema version comment.

### Environment / Secrets
- Never commit `.env` files or private keys.
- The `market-data.db` SQLite file is a runtime artifact — do not commit it.

### Tests
- **No test framework is configured.** The `npm test` script is a placeholder that exits with an error.
- When adding tests, choose a lightweight framework like `jest` or `mocha` and update `package.json` accordingly.

### No Build Step
- There is no TypeScript compilation, bundling, or transpilation.
- Run directly with `node market-data-collector.js`.

---

## Data Exports

CSV exports in `data_exports/` are manually generated snapshots committed to the repo for reference and offline analysis. They are not auto-generated by the collector loop.

---

## Git Workflow

- **Main branch:** `master`
- **Feature/session branches:** follow the pattern `claude/<description>-<session-id>`
- Commit messages are short and descriptive (e.g., `v4: add last_price from trades API`)
- No CI/CD pipeline exists; changes are manually tested by running the script

---

## Known Gaps / Future Work

- No `.env.example` — document required environment variables
- No test suite
- No linting/formatting enforcement
- No CI/CD
- README.md is essentially empty
- `btc_eth_up_with_outcome_prices.csv` is currently an empty file
