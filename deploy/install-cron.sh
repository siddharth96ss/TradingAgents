#!/usr/bin/env bash
# Install the TradingAgents cron job.
# Usage: bash install-cron.sh [schedule]
#   schedule: cron expression (default: "30 10 * * 1-5" = 10:30 AM UTC, 4:00 PM IST, weekdays only)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCHEDULE="${1:-30 10 * * 1-5}"
CRON_CMD="$SCRIPT_DIR/cron-wrapper.sh"
LOG_FILE="$SCRIPT_DIR/../logs/cron/cron_install.log"

mkdir -p "$(dirname "$LOG_FILE")"

# Make wrapper executable
chmod +x "$CRON_CMD"

# Build the cron line
CRON_LINE="$SCHEDULE /usr/bin/bash $CRON_CMD >> $LOG_FILE 2>&1"

# Check if already installed
if crontab -l 2>/dev/null | grep -q "cron-wrapper.sh"; then
    echo "Cron job already exists. Updating..."
    crontab -l 2>/dev/null | grep -v "cron-wrapper.sh" | { cat; echo "$CRON_LINE"; } | crontab -
else
    echo "Installing new cron job..."
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
fi

echo "=== Cron Job Installed ==="
echo "Schedule: $SCHEDULE"
echo "Command:  $CRON_CMD"
echo "Logs:     $LOG_FILE"
echo ""
echo "Verify with:  crontab -l"
echo "Test now:     bash $CRON_CMD"
echo ""
echo "Schedule examples:"
echo "  Weekdays 4 PM IST (10:30 UTC):  30 10 * * 1-5"
echo "  Every day 6 AM UTC:             0 6 * * *"
echo "  Weekdays 9 AM UTC (2:30 PM IST): 30 9 * * 1-5"
