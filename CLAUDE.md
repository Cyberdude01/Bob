# Claude Code — System Notes for Polymarket Bob

## Architecture Overview

### Signal Pipeline (the "brain")
- **`feed-updater.js`** — lives at `/root/Bob/feed-updater.js` on the droplet (NOT in git — needs to be committed)
- Runs every 5 minutes via cron: `*/5 * * * * cd /root/Bob && node feed-updater.js >> /root/Bob/feed-updater.log 2>&1`
- Fetches live BTC/ETH/SOL/XRP 15-min market data, calculates probabilities & signals, writes `data_exports/signals.json` + `data_exports/markets.json`, commits and pushes to `origin/main`
- Commit author: `Polymarket Feed <polymarket-feed@bot>`, commit message format: `data: YYYY-MM-DDTHH:MM ET`
- **Critical**: It pushes to whatever the current git branch of `/root/Bob` is. If the repo is on a non-main branch, signals never reach `origin/main` and all trading stops.

### Trade Executor
- **`run_executor.sh`** — pulls `signals.json` + `markets.json` from `origin/main`, then runs `python3 -m polymarket.trade_executor --execute`
- Runs every 5 minutes via systemd: `poly-executor.timer` → `poly-executor.service`
- **Staleness guard**: rejects signals older than 4 hours (`MAX_SIGNAL_AGE_HOURS = 4` in `trade_executor.py`)
- Deduplication: executed signals tracked in `data_exports/executed.json`

### Auto-Redeem
- `poly-auto-redeem.service` — runs continuously, redeems winning CTF positions on-chain every 10 minutes
- Independent of the signal pipeline — keeps running even if trading is broken

### Other services on droplet
- `polymarket.service` — runs `python -m polymarket` from `/root`, WorkingDirectory `/root` — separate legacy service
- `polymarket-bot/` at `/root/polymarket-bot/` — older JS bot (cron: `viem-redeem.js`, `collect-market-data.js`)
- `market-data-collector.js` — collects raw price data into `market-data.db` (SQLite)

---

## Known Issues & Fixes

### 1. `python: command not found` (exit 127)
- **Cause**: `run_executor.sh` called `python` but droplet only has `python3`
- **Fix**: Changed to `python3` in `run_executor.sh` (committed to `claude/fix-smoke-test-issues-bMXTv`)

### 2. Signals stuck / no trades placed
- **Symptom**: `signals.json` timestamp is days old, executor logs `signals.json is Xh old`
- **Primary cause**: `/root/Bob` repo on the droplet got checked out to a Claude session branch (e.g. `claude/fix-github-feed-A3ye3`). `feed-updater.js` pushes to current branch, so `origin/main` never updates.
- **Fix**: On the droplet: `cd /root/Bob && git checkout main && git pull origin main`
- `run_executor.sh` always pulls from `origin/main` regardless of local branch

### 3. `feed-updater.js` push rejected
- **Symptom**: `! [rejected] claude/fix-... -> claude/fix-... (fetch first)` in `feed-updater.log`
- **Cause**: Same as above — wrong branch + remote has diverged commits
- **Fix**: Switch droplet repo back to `main`

---

## Droplet Details
- Host: `debian-s-1vcpu-1gb-tor1-01` (DigitalOcean, Toronto)
- Working directory: `/root/Bob`
- Python venv: `/root/venv/bin/python3`
- Key log: `journalctl -u poly-executor -n 50 --no-pager`
- Feed log: `tail -f /root/Bob/feed-updater.log`

## TODO — Important
- **Commit `feed-updater.js` to git** — it only exists on the droplet filesystem; a rebuild would lose it
- Run `cat /root/Bob/feed-updater.js` and commit it to preserve the signal generation logic

---

## Development Branch
- Active branch: `claude/fix-smoke-test-issues-bMXTv`
- Always push to this branch; never push to main directly
