#!/usr/bin/env bash
# Install the TradingAgents Telegram bot as a systemd service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_FILE="$SCRIPT_DIR/tradingagents-bot.service"
TARGET="/etc/systemd/system/tradingagents-bot.service"

echo "=== Installing TradingAgents Bot Service ==="

# Install Python dependencies for polling (no extra deps needed — uses requests)
echo "[1/3] Installing bot dependencies..."
pip3 install --quiet python-dotenv requests 2>/dev/null || true

# Copy service file
echo "[2/3] Installing systemd service..."
sudo cp "$SERVICE_FILE" "$TARGET"
sudo systemctl daemon-reload
sudo systemctl enable tradingagents-bot

# Start the bot
echo "[3/3] Starting bot..."
sudo systemctl restart tradingagents-bot

echo ""
echo "=== Bot Service Installed ==="
echo "Status:  sudo systemctl status tradingagents-bot"
echo "Logs:    journalctl -u tradingagents-bot -f"
echo "Restart: sudo systemctl restart tradingagents-bot"
echo "Stop:    sudo systemctl stop tradingagents-bot"
