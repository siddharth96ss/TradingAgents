# TradingAgents VM Setup Guide

## SSH Connection

```bash
ssh -i ~/.ssh/oracle-trading ubuntu@129.225.97.232
```

## What's Running

| Service | Schedule | Command |
|---------|----------|---------|
| Daily stock scan | Weekdays 4 PM IST | `python3 -m tradingagents.batch_runner` |
| Telegram bot | Always on | `python3 -m tradingagents.notifications.telegram_bot` |
| Auto-update | Every 15 min | `git pull && systemctl restart tradingagents-bot` |

## Useful Commands

```bash
# Check scan status
crontab -l

# Check bot status
sudo systemctl status tradingagents-bot

# View scan logs
cat ~/tradingagents/logs/cron/scan_$(date +%Y-%m-%d).log

# View bot logs
sudo journalctl -u tradingagents-bot -f

# Manual scan
cd ~/tradingagents && python3 -m tradingagents.batch_runner

# Restart bot
sudo systemctl restart tradingagents-bot

# Pull latest code
cd ~/tradingagents && git pull origin indian-market
```

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/status` | Today's scan overview |
| `/signals` | Buy/overweight signals |
| `/errors` | Any errors from last scan |
| `/last` | Last analyzed stock details |
| `/history` | Last 5 days summary |
| `/run` | Trigger a manual scan |

## VM Details

- **IP:** 129.225.97.232
- **Shape:** VM.Standard.E2.1.Micro (1 GB RAM)
- **OS:** Ubuntu 22.04
- **SSH Key:** `~/.ssh/oracle-trading`
- **Repo:** `~/tradingagents` (branch: `indian-market`)
