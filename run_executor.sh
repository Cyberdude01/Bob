#!/bin/bash
# run_executor.sh — Pull fresh signals from origin/main, then run trade executor
set -e

cd "$(dirname "$0")"

# ── Trading pause guard ────────────────────────────────────────────────────────
# To pause ALL trade execution without stopping the feed:
#   touch /root/Bob/.TRADING_PAUSED
# To resume:
#   rm /root/Bob/.TRADING_PAUSED
# The feed-updater keeps running so signals.json stays fresh.
if [ -f ".TRADING_PAUSED" ]; then
  echo "[run_executor] TRADING PAUSED — .TRADING_PAUSED file present. No orders will be placed."
  echo "[run_executor] Remove /root/Bob/.TRADING_PAUSED to resume trading."
  exit 0
fi

echo "[run_executor] Pulling latest data from origin/main..."
# Remove stale git lock file if present (can be left behind by crashed git processes)
if [ -f ".git/index.lock" ]; then
  echo "[run_executor] WARNING: Removing stale .git/index.lock"
  rm -f ".git/index.lock"
fi
git fetch origin main
git checkout origin/main -- data_exports/signals.json data_exports/markets.json

echo "[run_executor] Running trade executor..."
/root/venv/bin/python3 -m polymarket.trade_executor "$@"

echo "[run_executor] Running order settler..."
/root/venv/bin/python3 -m polymarket.settle_orders
