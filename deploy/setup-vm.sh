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
echo "[1/6] Updating system packages..."
sudo apt-get update -qq && sudo apt-get upgrade -y -qq

# 2. Install Docker
echo "[2/6] Installing Docker..."
if ! command -v docker &>/dev/null; then
    sudo apt-get install -y -qq docker.io docker-compose-v2
    sudo systemctl enable --now docker
    sudo usermod -aG docker "$USER"
    echo "Docker installed. You may need to log out/in for group changes."
else
    echo "Docker already installed."
fi

# 3. Install Python 3.12+ and git
echo "[3/6] Installing Python and git..."
sudo apt-get install -y -qq python3 python3-pip python3-venv git

# 4. Clone the repo
echo "[4/6] Cloning TradingAgents..."
if [ -d "$INSTALL_DIR" ]; then
    echo "Directory $INSTALL_DIR already exists — pulling latest."
    cd "$INSTALL_DIR" && git checkout "$BRANCH" && git pull
else
    git clone -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 5. Create .env from template if it doesn't exist
echo "[5/6] Setting up .env..."
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

# 6. Build Docker image
echo "[6/6] Building Docker image..."
docker compose build

echo ""
echo "=== Setup Complete ==="
echo "Next steps:"
echo "  1. Edit .env:  nano $INSTALL_DIR/.env"
echo "  2. Test run:   cd $INSTALL_DIR && docker compose run --rm tradingagents-batch"
echo "  3. Install cron: bash $INSTALL_DIR/deploy/install-cron.sh"
