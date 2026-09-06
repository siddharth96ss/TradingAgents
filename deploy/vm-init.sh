#!/usr/bin/env bash
# All-in-one TradingAgents setup for Oracle Cloud Free VM.
# Run this ON THE VM after SSH'ing in.
# Usage: bash deploy/vm-init.sh
#
# What it does:
#   1. Installs Docker, Python, git
#   2. Clones TradingAgents (indian-market branch)
#   3. Prompts for API keys and writes .env
#   4. Builds Docker image
#   5. Runs a quick test
#   6. Installs weekday cron job (4 PM IST)
set -euo pipefail

REPO_URL="https://github.com/siddharth96ss/TradingAgents.git"
BRANCH="indian-market"
INSTALL_DIR="$HOME/tradingagents"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

echo ""
echo "=========================================="
echo "  TradingAgents - Oracle Cloud VM Setup"
echo "=========================================="
echo ""

# ---- Step 1: System packages ----
info "[1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq docker.io docker-compose-v2 python3 python3-pip python3-venv git curl
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" 2>/dev/null || true
ok "System packages installed."

# ---- Step 2: Clone repo ----
info "[2/7] Cloning TradingAgents..."
if [ -d "$INSTALL_DIR" ]; then
    cd "$INSTALL_DIR" && git checkout "$BRANCH" && git pull --quiet
    ok "Repo updated."
else
    git clone -b "$BRANCH" --quiet "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    ok "Repo cloned."
fi

# ---- Step 3: Collect API keys ----
info "[3/7] Configuring API keys..."
echo ""
echo "I need your API keys. Press Enter to skip any you don't have yet."
echo "You can always edit ~/tradingagents/.env later."
echo ""

read -rp "OpenRouter API Key: " OPENROUTER_KEY
read -rp "Telegram Bot Token: " TG_TOKEN
read -rp "Telegram Chat ID: " TG_CHATID

# Build .env
cat > .env <<ENVEOF
# LLM Provider
OPENROUTER_API_KEY=${OPENROUTER_KEY}

# TradingAgents config
TRADINGAGENTS_LLM_PROVIDER=openrouter
TRADINGAGENTS_DEEP_THINK_LLM=minimax/minimax-m3:free
TRADINGAGENTS_QUICK_THINK_LLM=minimax/minimax-m3:free

# Telegram notifications
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_CHAT_ID=${TG_CHATID}
ENVEOF

chmod 600 .env
ok ".env written."

# ---- Step 4: Build Docker image ----
info "[4/7] Building Docker image (this takes a few minutes)..."
docker compose build --quiet
ok "Docker image built."

# ---- Step 5: Quick smoke test ----
info "[5/7] Running smoke test..."
if docker compose run --rm tradingagents-batch --dry-run 2>&1 | tail -5; then
    ok "Smoke test passed."
else
    warn "Smoke test had issues — check output above. Continuing anyway."
fi

# ---- Step 6: Install cron ----
info "[6/7] Installing cron job (weekdays 4 PM IST)..."
CRON_CMD="$INSTALL_DIR/deploy/cron-wrapper.sh"
chmod +x "$CRON_CMD"
LOG_FILE="$INSTALL_DIR/logs/cron/cron_install.log"
mkdir -p "$(dirname "$LOG_FILE")"

CRON_LINE="30 10 * * 1-5 /usr/bin/bash $CRON_CMD >> $LOG_FILE 2>&1"

if crontab -l 2>/dev/null | grep -q "cron-wrapper.sh"; then
    crontab -l 2>/dev/null | grep -v "cron-wrapper.sh" | { cat; echo "$CRON_LINE"; } | crontab -
    ok "Cron job updated."
else
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    ok "Cron job installed."
fi

# ---- Step 7: Summary ----
echo ""
echo "=========================================="
echo "  Setup Complete!"
echo "=========================================="
echo ""
echo "  Repo:     $INSTALL_DIR"
echo "  Branch:   $BRANCH"
echo "  Cron:     Weekdays 10:30 UTC (4:00 PM IST)"
echo "  Logs:     $INSTALL_DIR/logs/cron/"
echo ""
echo "  Test now:    cd ~/tradingagents && docker compose run --rm tradingagents-batch"
echo "  Check cron:  crontab -l"
echo "  View logs:   tail -f ~/tradingagents/logs/cron/scan_\$(date +%Y-%m-%d).log"
echo "  Edit .env:   nano ~/tradingagents/.env"
echo ""
echo "  You'll get Telegram alerts for:"
echo "    - Buy/Overweight signals (immediately)"
echo "    - Daily scan summary (end of scan)"
echo "    - Errors/crashes (if something fails)"
echo ""
