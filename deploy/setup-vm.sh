#!/usr/bin/env bash
# Oracle Cloud Free VM setup script for TradingAgents.
# Run this ON THE VM after SSH'ing in.
# Usage: bash setup-vm.sh <github-repo-url> [branch]
set -euo pipefail

REPO_URL="${1:?Usage: bash setup-vm.sh <github-repo-url> [branch]}"
BRANCH="${2:-indian-market}"
INSTALL_DIR="$HOME/tradingagents"

echo "=== TradingAgents VM Setup ==="

# 1. System updates
echo "[1/5] Updating system packages..."
sudo apt-get update -qq && sudo apt-get upgrade -y -qq

# 2. Install Python, pip, git
echo "[2/5] Installing Python and git..."
sudo apt-get install -y -qq python3 python3-pip python3-venv python3-dev git

# 3. Clone the repo
echo "[3/5] Cloning TradingAgents..."
if [ -d "$INSTALL_DIR" ]; then
    echo "Directory $INSTALL_DIR already exists — pulling latest."
    cd "$INSTALL_DIR" && git checkout "$BRANCH" && git pull
else
    git clone -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 4. Install Python dependencies
echo "[4/5] Installing Python dependencies..."
pip3 install --break-system-packages -e "$INSTALL_DIR" 2>/dev/null || \
pip3 install -e "$INSTALL_DIR"

# 5. Create .env from template if it doesn't exist
echo "[5/5] Setting up .env..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo ">>> IMPORTANT: Edit .env with your API keys <<<
    >>>   nano $INSTALL_DIR/.env
    >>>
    >>> Required keys:
    >>>   OPENROUTER_API_KEY (or whichever provider you use)
    >>>   TELEGRAM_BOT_TOKEN
    >>>   TELEGRAM_CHAT_ID"
    echo ""
else
    echo ".env already exists."
fi

echo ""
echo "=== Setup Complete ==="
echo "Next steps:"
echo "  1. Edit .env:  nano $INSTALL_DIR/.env"
echo "  2. Test run:   cd $INSTALL_DIR && python3 -m tradingagents.batch_runner --dry-run"
echo "  3. Install cron + bot: bash $INSTALL_DIR/deploy/install-cron.sh && bash $INSTALL_DIR/deploy/install-bot.sh"
