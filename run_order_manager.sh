#!/bin/bash
# run_order_manager.sh — Launch the continuous order placement daemon
set -e

cd "$(dirname "$0")"

echo "[run_order_manager] Starting Polymarket Order Manager..."
exec /root/venv/bin/python3 -m polymarket.order_manager
