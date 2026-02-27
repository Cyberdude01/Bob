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
├── .env.example               # Required environment variable template
├── prompts/                   # Modular subagent prompt templates
│   ├── spec-agent.md
│   ├── planner-agent.md
│   ├── coder-agent.md
│   ├── test-writer-agent.md
│   ├── reviewer-agent.md
│   └── simplifier-agent.md
├── tests/                     # Jest test suite
│   └── market-data.test.js
├── market-data.db             # SQLite database (runtime artifact, not committed)
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
| Test framework | Jest |

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
Copy `.env.example` to `.env` and fill in values:
```bash
cp .env.example .env
```

### Running the Collector
```bash
node market-data-collector.js
```
The script starts immediately, collects data, then repeats every **60 seconds**.

### Running Tests
```bash
npm test
```

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
- Test framework: **Jest** (see `tests/` directory).
- Run with `npm test`.
- The run-fix-rerun loop applies: write code → run tests → fix failures → re-run until all pass.
- Never mark a task complete while tests are failing.

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

- No CI/CD
- README.md is essentially empty
- `btc_eth_up_with_outcome_prices.csv` is currently an empty file

---

---

# Agent Workflow Architecture

> This section defines how AI agents (Claude or any LLM assistant) should operate on this codebase.
> The core principle: **the bottleneck is human attention, not model speed.**
> Every design decision reduces the number of times a human must intervene.

---

## The Meta-Rule: Plan Before You Act

**Never write a single line of code before the spec is confirmed and the plan is approved.**

This prevents the classic failure mode: an agent makes 40 changes the human didn't want.
The sequence is always: Spec → Plan → Execute. No shortcuts.

---

## The Full Agent Loop

```
Requirement IN
    ↓
[1] Spec Agent
    → Restate requirement in own words
    → Identify ambiguities, edge cases, contradictions
    → Propose 2–3 interpretations
    → WAIT for human to select one
    → Output: confirmed spec (JSON)
    ↓
[2] Planner Agent
    → Generate ordered steps with dependencies
    → Define success criteria per step
    → Flag risks per step
    → WAIT for human (or Critic Agent) to approve
    → Output: approved plan (JSON)
    ↓
[3] Parallel Subagents execute
    → Coder Agent: implements one plan step at a time
    → Test Writer Agent: writes tests for that unit
    → (run independently, results handed off via JSON schema)
    ↓
[4] Code Execution Loop (per step)
    → Write code
    → Run npm test
    → PASS → continue
    → FAIL → identify failing assertion → root-cause → patch → re-run
    → Retry limit: 5 attempts; escalate to human if still failing
    ↓
[5] Reviewer Agent
    → Compare final output against original confirmed spec
    → Produce pass/fail verdict with line-level explanation
    → Any delta from spec → flag for human review, do not silently accept
    ↓
[6] Output delivered
    ↓
[7] Human reviews PR / output
    → Any error pattern spotted → written into CLAUDE.md (Lessons Learned section)
    → CLAUDE.md update = next session starts smarter
```

---

## Subagent Roles

Each role is a **narrow, focused concern**. Do not combine roles in one context.
Prompt templates live in `prompts/`.

| Agent | File | Concern | Max context |
|---|---|---|---|
| Spec Agent | `prompts/spec-agent.md` | Validate & clarify the requirement | ~1500 tokens |
| Planner Agent | `prompts/planner-agent.md` | Generate & validate the build plan | ~2000 tokens |
| Coder Agent | `prompts/coder-agent.md` | Implement one plan step | ~2000 tokens |
| Test Writer Agent | `prompts/test-writer-agent.md` | Write Jest tests for one unit | ~1500 tokens |
| Reviewer Agent | `prompts/reviewer-agent.md` | Verify output matches confirmed spec | ~1500 tokens |
| Simplifier Agent | `prompts/simplifier-agent.md` | Reduce complexity after long sessions | ~1500 tokens |

---

## Feedback Loops

### Immediate Loop (within a task)
When a test fails:
1. Identify the exact failing assertion and error message.
2. Root-cause the failure (logic error, wrong data shape, missing mock, etc.).
3. Patch the minimal code needed to fix it.
4. Re-run `npm test`.
5. Repeat up to 5 times. If still failing after 5 attempts, **stop and ask the human**.

Do not brute-force. Do not make speculative changes hoping something sticks.

### Session Loop (end of task)
After all steps are complete:
- Reviewer Agent runs against the confirmed spec.
- Any mismatch is flagged before the PR is created.
- Do not mark the task complete until the Reviewer Agent produces a clean pass.

### Persistent Loop (across sessions)
When a human reviewer catches an error pattern during PR review or QA:
- That pattern becomes a standing rule in the **Lessons Learned** section below.
- The rule is written as a clear, actionable constraint: "Never do X; do Y instead."
- This makes each session smarter than the last.

---

## Token Management

### Scope subagents narrowly
Each agent holds only the context relevant to its concern.
- The Coder Agent does not need the full spec history — only the current plan step and the relevant function signature.
- The Test Writer Agent does not need the database schema — only the function under test and its expected outputs.

### Use CLAUDE.md as compressed external memory
Short, declarative rules here are far more token-efficient than re-prompting conventions each session.
Add rules here as they are discovered. Do not re-explain them inline.

### Summarize, don't replay
For long-running tasks, compress earlier context into a state snapshot before hitting context limits.
Pass the snapshot (not the full history) to the next agent or session.

### Checkpoint after each plan step
Break execution at each plan step boundary. Start the next step with a fresh context that carries only:
- The confirmed spec (JSON)
- The approved plan (JSON, current step highlighted)
- The test results from the previous step

### Model matching
| Task type | Recommended model |
|---|---|
| Spec validation, planning, review | Most capable available (e.g., claude-opus-4-6) |
| Code generation, test writing | Balanced (e.g., claude-sonnet-4-6) |
| Simple lookups, formatting, summarization | Fastest/cheapest (e.g., claude-haiku-4-5) |

### Token budget guard
If context for a single step exceeds ~1500 tokens of accumulated history, trigger a Summarizer Agent to compress before continuing.

---

## Handoff Schema

Agents communicate via structured JSON to keep handoffs token-efficient and unambiguous.

### Spec Agent output
```json
{
  "requirement_restated": "string",
  "ambiguities": ["string"],
  "selected_interpretation": "string",
  "out_of_scope": ["string"],
  "human_confirmed": true
}
```

### Planner Agent output
```json
{
  "steps": [
    {
      "id": 1,
      "description": "string",
      "depends_on": [],
      "success_criteria": ["string"],
      "risks": ["string"]
    }
  ],
  "human_approved": true
}
```

### Coder Agent output
```json
{
  "step_id": 1,
  "files_changed": ["string"],
  "test_result": "pass | fail",
  "test_output": "string",
  "notes": "string"
}
```

### Reviewer Agent output
```json
{
  "verdict": "pass | fail",
  "spec_alignment": ["pass: item", "fail: item"],
  "deltas": ["string"],
  "requires_human_review": true
}
```

---

## Lessons Learned

> Standing rules derived from past error patterns. Every mistake becomes an instruction.
> Add new rules here after PR review or QA catches a recurring issue.

*(No entries yet — add the first when a pattern is identified.)*

---

## Error Pattern Registry

| Pattern | Rule |
|---|---|
| *(none yet)* | *(add here as patterns emerge)* |
