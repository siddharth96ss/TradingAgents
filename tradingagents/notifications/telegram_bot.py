"""Interactive Telegram bot for querying scan results.

Long-running service that polls Telegram for commands and responds
with scan data from the daily progress files.

Usage:
    python -m tradingagents.notifications.telegram_bot
    tradingagents-bot  (if installed as console script)

Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.telegram.org/bot{token}"
_POLL_TIMEOUT = 30  # long-poll timeout in seconds
_COOLDOWN = 2  # seconds between command processing


def _get_config():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    return token, chat_id


def _results_dir() -> Path:
    home = Path.home()
    env_dir = os.getenv("TRADINGAGENTS_RESULTS_DIR")
    if env_dir:
        return Path(env_dir)
    return home / ".tradingagents" / "logs"


def _send(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    url = f"{_BASE_URL.format(token=token)}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error("Telegram API error %s: %s", resp.status_code, resp.text)
            return False
        return True
    except Exception as exc:
        logger.error("Failed to send: %s", exc)
        return False


def _get_updates(token: str, offset: Optional[int] = None) -> list:
    url = f"{_BASE_URL.format(token=token)}/getUpdates"
    params = {"timeout": _POLL_TIMEOUT}
    if offset:
        params["offset"] = offset
    try:
        resp = requests.get(url, params=params, timeout=_POLL_TIMEOUT + 10)
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception as exc:
        logger.error("Polling error: %s", exc)
    return []


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_progress(date: str) -> dict:
    progress_file = _results_dir() / "daily" / f"{date}_progress.json"
    if progress_file.exists():
        with open(progress_file, encoding="utf-8") as f:
            return json.load(f)
    return {"analyzed": [], "signals": [], "errors": [], "started_at": None}


def _cmd_start() -> str:
    return (
        "🟢 <b>TradingAgents Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Commands:\n"
        "  /status — Today's scan overview\n"
        "  /signals — Buy/overweight signals\n"
        "  /errors — Any errors from last scan\n"
        "  /last — Details of last analyzed stock\n"
        "  /history — Last 5 days summary\n"
        "  /run — Trigger a scan now\n"
        "  /help — Show this message"
    )


def _cmd_status() -> str:
    progress = _load_progress(_today())
    analyzed = len(progress.get("analyzed", []))
    signals = len(progress.get("signals", []))
    errors = len(progress.get("errors", []))
    started = progress.get("started_at", "Not started yet")
    completed = progress.get("completed_at", "Not completed")

    return (
        f"📊 <b>Today's Scan — {_today()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Stocks Analyzed: {analyzed}\n"
        f"Buy Signals: {signals}\n"
        f"Errors: {errors}\n"
        f"Started: {started[:19] if started != 'Not started yet' else started}\n"
        f"Completed: {completed[:19] if completed != 'Not completed' else completed}"
    )


def _cmd_signals() -> str:
    progress = _load_progress(_today())
    signals = progress.get("signals", [])

    if not signals:
        return f"📭 No buy signals found today ({_today()})."

    lines = [f"🟢 <b>Buy Signals — {_today()}</b>", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    for sig in signals:
        ticker = sig.get("ticker", "?")
        rating = sig.get("rating", "?")
        pt = sig.get("price_target")
        entry = sig.get("entry_price")
        sl = sig.get("stop_loss")
        pt_str = f" | Target: ₹{pt:,.2f}" if pt else ""
        entry_str = f"Entry: ₹{entry:,.2f}" if entry else ""
        sl_str = f"SL: ₹{sl:,.2f}" if sl else ""
        levels = " | ".join(filter(None, [entry_str, sl_str]))

        lines.append(f"\n<b>{ticker}</b> — {rating}{pt_str}")
        if levels:
            lines.append(f"  {levels}")

    return "\n".join(lines)


def _cmd_errors() -> str:
    progress = _load_progress(_today())
    errors = progress.get("errors", [])

    if not errors:
        return f"✅ No errors today ({_today()})."

    lines = [f"⚠️ <b>Errors — {_today()}</b>", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    for i, err in enumerate(errors[:10], 1):
        truncated = err[:200] + "..." if len(err) > 200 else err
        lines.append(f"{i}. {truncated}")
    if len(errors) > 10:
        lines.append(f"\n... and {len(errors) - 10} more")

    return "\n".join(lines)


def _cmd_last() -> str:
    progress = _load_progress(_today())
    analyzed = progress.get("analyzed", [])
    signals = progress.get("signals", [])

    if not analyzed:
        return "📭 No stocks analyzed today."

    last_ticker = analyzed[-1]
    last_signal = None
    for sig in signals:
        if sig.get("ticker") == last_ticker:
            last_signal = sig
            break

    # Try to load the full report
    report_dir = _results_dir() / "daily" / _today() / last_ticker
    report_files = sorted(report_dir.glob("*.md")) if report_dir.exists() else []

    lines = [f"📋 <b>Last Analyzed: {last_ticker}</b>", "━━━━━━━━━━━━━━━━━━━━━━━━"]

    if last_signal:
        lines.append(f"Rating: {last_signal.get('rating', '?')}")
        if last_signal.get("price_target"):
            lines.append(f"Target: ₹{last_signal['price_target']:,.2f}")
        if last_signal.get("entry_price"):
            lines.append(f"Entry: ₹{last_signal['entry_price']:,.2f}")
        if last_signal.get("stop_loss"):
            lines.append(f"Stop Loss: ₹{last_signal['stop_loss']:,.2f}")
        summary = last_signal.get("summary", "")
        if summary:
            lines.append(f"\n{summary[:400]}")
    else:
        # No signal data — try to read from report file
        if report_files:
            with open(report_files[-1], encoding="utf-8") as f:
                content = f.read()
            # Find the rating line
            for line in content.split("\n"):
                if "Rating" in line and ":" in line:
                    lines.append(line.strip())
                    break
            lines.append(f"\nReport: {report_files[-1].name}")
        else:
            lines.append(f"Analyzed but no report saved.")

    return "\n".join(lines)


def _cmd_history() -> str:
    daily_dir = _results_dir() / "daily"
    if not daily_dir.exists():
        return "📭 No scan history found."

    lines = ["📅 <b>Last 5 Days</b>", "━━━━━━━━━━━━━━━━━━━━━━━━"]

    date_dirs = sorted([d for d in daily_dir.iterdir() if d.is_dir()], reverse=True)[:5]
    progress_files = sorted(daily_dir.glob("*_progress.json"), reverse=True)[:5]

    for pf in progress_files:
        date_str = pf.stem.replace("_progress", "")
        with open(pf, encoding="utf-8") as f:
            data = json.load(f)
        analyzed = len(data.get("analyzed", []))
        signals = len(data.get("signals", []))
        errors = len(data.get("errors", []))
        lines.append(f"<b>{date_str}</b>: {analyzed} stocks, {signals} signals, {errors} errors")

    if not progress_files:
        lines.append("No progress files found.")

    return "\n".join(lines)


def _cmd_run() -> str:
    """Trigger a manual scan (non-blocking)."""
    return (
        "🚀 Starting manual scan...\n"
        "This may take a while. You'll get a Telegram alert when buy signals are found.\n"
        "Check /status for progress."
    )


def _handle_command(command: str, token: str, chat_id: str) -> None:
    command = command.strip().lower()

    if command in ("/start", "/help"):
        text = _cmd_start()
    elif command == "/status":
        text = _cmd_status()
    elif command == "/signals":
        text = _cmd_signals()
    elif command == "/errors":
        text = _cmd_errors()
    elif command == "/last":
        text = _cmd_last()
    elif command == "/history":
        text = _cmd_history()
    elif command == "/run":
        text = _cmd_run()
        _send(token, chat_id, text)
        # Trigger scan in background
        subprocess.Popen(
            ["docker", "compose", "-f", "/home/ubuntu/tradingagents/docker-compose.yml",
             "run", "--rm", "tradingagents-batch"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    else:
        text = f"Unknown command: {command}\n\nType /help for available commands."

    _send(token, chat_id, text)


def run_bot():
    """Main polling loop."""
    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    token, chat_id = _get_config()
    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        sys.exit(1)

    logger.info("Bot starting. Chat ID: %s", chat_id)

    # Send startup message
    _send(token, chat_id, "🟢 <b>TradingAgents Bot</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\nOnline. Type /help for commands.")

    offset = None
    while True:
        try:
            updates = _get_updates(token, offset)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message", {})
                text = message.get("text", "")
                if text and text.startswith("/"):
                    sender_chat_id = str(message.get("chat", {}).get("id", ""))
                    if sender_chat_id == chat_id:
                        logger.info("Command from %s: %s", sender_chat_id, text)
                        _handle_command(text, token, chat_id)
                    else:
                        logger.warning("Unauthorized message from %s", sender_chat_id)
        except KeyboardInterrupt:
            logger.info("Bot stopped.")
            _send(token, chat_id, "🔴 <b>TradingAgents Bot</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\nOffline.")
            break
        except Exception as exc:
            logger.error("Loop error: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
