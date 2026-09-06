#!/usr/bin/env bash
# Cron wrapper for TradingAgents daily scan.
# Runs the batch runner with Python and sends Telegram alerts on failure.
# Logs everything to a daily log file.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs/cron"
TODAY="$(date +%Y-%m-%d)"
LOG_FILE="$LOG_DIR/scan_${TODAY}.log"

mkdir -p "$LOG_DIR"

# Source .env for Telegram credentials
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

send_telegram_error() {
    local message="$1"
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            -d "parse_mode=HTML" \
            -d "text=🔴 <b>CRON FAILURE</b>
━━━━━━━━━━━━━━━━━━━━━━━━
${message}

$(date '+%Y-%m-%d %H:%M')" \
            >/dev/null 2>&1
    fi
}

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg" | tee -a "$LOG_FILE"
}

log "Starting daily scan..."

# Run the batch runner with Python
if cd "$PROJECT_DIR" && python3 -m tradingagents.batch_runner 2>&1 | tee -a "$LOG_FILE"; then
    log "Scan completed successfully."
else
    EXIT_CODE=$?
    ERROR_MSG="Batch runner exited with code $EXIT_CODE. Check logs: $LOG_FILE"
    log "ERROR: $ERROR_MSG"
    send_telegram_error "$ERROR_MSG"
    exit $EXIT_CODE
fi
