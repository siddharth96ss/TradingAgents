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
import signal
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
_PID_FILE = Path("/tmp/tradingagents_bot.pid")


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


def _project_dir() -> Path:
    """Path to the TradingAgents repo on the host."""
    env_dir = os.getenv("TRADINGAGENTS_PROJECT_DIR")
    if env_dir:
        return Path(env_dir)
    # Default: ~/tradingagents (as deployed by vm-init.sh)
    return Path.home() / "tradingagents"


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


def _is_scan_running() -> bool:
    """Check if the batch runner is currently executing."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "tradingagents.batch_runner"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _scan_uptime(progress: dict) -> str:
    """Calculate elapsed time since scan started."""
    started = progress.get("started_at")
    if not started:
        return ""
    try:
        start_dt = datetime.fromisoformat(started)
        elapsed = (datetime.now() - start_dt).total_seconds()
        mins, secs = divmod(int(elapsed), 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{mins}m {secs}s"
    except Exception:
        return ""


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
    analyzed = progress.get("analyzed", [])
    signals = progress.get("signals", [])
    errors = progress.get("errors", [])
    started = progress.get("started_at")
    completed = progress.get("completed_at")
    running = _is_scan_running()

    total_stocks = progress.get("total_stocks", 0)
    current_stock = progress.get("current_stock")
    current_index = progress.get("current_index", 0)

    # Determine status line
    if running:
        status_icon = "🔄 <b>SCAN IN PROGRESS</b>"
    elif completed:
        status_icon = "✅ <b>Scan completed</b>"
    elif started:
        status_icon = "⚠️ <b>Scan interrupted</b>"
    else:
        status_icon = "💤 No scan today"

    # Elapsed time
    uptime = _scan_uptime(progress)
    elapsed_line = f"Elapsed: {uptime}\n" if uptime else ""

    # Current stock
    current_line = ""
    if running and current_stock:
        idx = f"{current_index}/{total_stocks}" if total_stocks else ""
        current_line = f"Analyzing: {current_stock} ({idx})\n"

    # Progress bar
    if total_stocks > 0:
        progress_pct = len(analyzed) / total_stocks * 100
        bar_len = 10
        filled = int(bar_len * min(progress_pct, 100) / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        progress_line = f"[{bar}] {len(analyzed)}/{total_stocks}\n"
    elif running:
        progress_line = f"Analyzing... {len(analyzed)} done\n"
    else:
        progress_line = ""

    # Format timestamps
    def _fmt(val: str) -> str:
        if not val:
            return "—"
        return val[:19]

    started_str = _fmt(started) if started else "—"
    completed_str = _fmt(completed) if completed else "In progress..." if running else "—"

    return (
        f"📊 <b>Today's Scan — {_today()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{status_icon}\n"
        f"{elapsed_line}"
        f"{current_line}"
        f"{progress_line}"
        f"Analyzed: {len(analyzed)}\n"
        f"Buy Signals: {len(signals)}\n"
        f"Errors: {len(errors)}\n"
        f"Started: {started_str}\n"
        f"Completed: {completed_str}"
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
    """Trigger a manual scan (non-blocking) using Python directly."""
    if _is_scan_running():
        return "⏳ A scan is already running. Check /status for progress."

    project_dir = _project_dir()
    log_dir = _results_dir() / "daily" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = log_dir / f"manual_{today}.log"

    # Source .env and run batch runner directly (like cron-wrapper.sh does)
    subprocess.Popen(
        f"source {project_dir}/.env && cd {project_dir} && "
        f"python3 -m tradingagents.batch_runner >> {log_file} 2>&1",
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        executable="/bin/bash",
    )

    return (
        "🚀 <b>Manual scan started</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Running Python batch runner directly.\n"
        "Check /status for live progress.\n"
        f"Logs: {log_file}"
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

    # Prevent duplicate instances via PID file lock
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            os.kill(old_pid, 0)  # Check if process is alive
            logger.error("Bot already running (PID %d). Exiting.", old_pid)
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass  # Old process dead, ok to continue
    _PID_FILE.write_text(str(os.getpid()))

    def _cleanup(*args):
        _PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    token, chat_id = _get_config()
    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        _PID_FILE.unlink(missing_ok=True)
        sys.exit(1)

    logger.info("Bot starting. Chat ID: %s, PID: %d", chat_id, os.getpid())

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
            _PID_FILE.unlink(missing_ok=True)
            break
        except Exception as exc:
            logger.error("Loop error: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
