#!/bin/bash
# run_executor.sh — Pull fresh signals from origin/main, then run trade executor
set -e

cd "$(dirname "$0")"

echo "[run_executor] Pulling latest data from origin/main..."
git fetch origin main
git checkout origin/main -- data_exports/signals.json data_exports/markets.json

echo "[run_executor] Running trade executor..."
python3 -m polymarket.trade_executor "$@"

echo "[run_executor] Running order settler..."
python3 -m polymarket.settle_orders
