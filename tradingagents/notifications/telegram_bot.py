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
import tempfile
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
        "  /report — View stock reports\n"
        "  /signals — Buy/overweight signals\n"
        "  /errors — Any errors from last scan\n"
        "  /last — Details of last analyzed stock\n"
        "  /history — Last 5 days summary\n"
        "  /run — Trigger a scan now\n"
        "  /help — Show this message"
    )


def _build_report_keyboard(stocks: list[dict]) -> str:
    """Build inline keyboard JSON for stock selection."""
    import json as _json
    buttons = []
    for stock in stocks:
        ticker = stock["ticker"]
        status = stock.get("status", "pending")
        if status == "completed":
            icon = "✅"
            signal = stock.get("signal", "")
            label = f"{icon} {ticker}" + (f" — {signal}" if signal else "")
        elif status == "in_progress":
            icon = "🔄"
            stage = stock.get("stage", "In Progress")
            label = f"{icon} {ticker} — {stage}"
        else:
            icon = "⏳"
            label = f"{icon} {ticker}"
        buttons.append([{"text": label, "callback_data": f"report:{ticker}"}])
    return _json.dumps({"inline_keyboard": buttons})


def _get_stock_list(progress: dict) -> list[dict]:
    """Build ordered stock list from progress data."""
    stocks_data = progress.get("stocks", {})
    analyzed = progress.get("analyzed", [])
    current_stock = progress.get("current_stock")

    # Build list: completed first (in order), then in-progress, then remaining
    result = []
    seen = set()

    # Completed stocks (in analysis order)
    for ticker in analyzed:
        if ticker in stocks_data:
            result.append({"ticker": ticker, **stocks_data[ticker]})
            seen.add(ticker)

    # Current in-progress stock
    if current_stock and current_stock not in seen:
        if current_stock in stocks_data:
            result.append({"ticker": current_stock, **stocks_data[current_stock]})
        else:
            result.append({"ticker": current_stock, "status": "in_progress", "stage": "Starting..."})
        seen.add(current_stock)

    # Pending stocks (from total_stocks if available, otherwise from analyzed list gap)
    total = progress.get("total_stocks", 0)
    if total > len(seen):
        # We don't know the pending tickers until they start, show count
        pending_count = total - len(seen)
        for i in range(pending_count):
            ticker = f"Pending #{i+1}"
            result.append({"ticker": ticker, "status": "pending"})
            seen.add(ticker)

    return result


def _cmd_report(token: str, chat_id: str, args: str = "") -> None:
    """Handle /report command — show stock picker or send specific report."""
    progress = _load_progress(_today())
    stocks = _get_stock_list(progress)

    if not stocks:
        _send(token, chat_id, f"📭 No stocks analyzed yet today ({_today()}).\n\nStart a scan with /run.")
        return

    # If args provided, try to find and send report for that stock
    if args:
        ticker = args.strip().upper()
        # Find matching stock
        matched = None
        for s in stocks:
            if s["ticker"].upper() == ticker or s["ticker"].upper().startswith(ticker):
                matched = s
                break

        if not matched:
            available = ", ".join(s["ticker"] for s in stocks if s["status"] != "pending")
            _send(token, chat_id, f"❌ {ticker} not found.\n\nAvailable: {available}")
            return

        _send_stock_report(token, chat_id, matched)
        return

    # Show stock picker with inline keyboard
    header = f"📋 <b>Today's Stocks ({_today()})</b>\nSelect a stock to view its report:"
    keyboard = _build_report_keyboard(stocks)
    from tradingagents.notifications.telegram import send_message_with_keyboard
    send_message_with_keyboard(header, keyboard)


def _send_stock_report(token: str, chat_id: str, stock: dict) -> None:
    """Send a stock's report as a file with a summary message."""
    ticker = stock["ticker"]
    status = stock.get("status", "pending")

    if status == "pending":
        _send(token, chat_id, f"⏳ {ticker} has not been analyzed yet.")
        return

    if status == "error":
        error = stock.get("error", "Unknown error")
        _send(token, chat_id, f"❌ {ticker} analysis failed:\n{error[:300]}")
        return

    # Build summary message
    signal = stock.get("signal", "")
    stage = stock.get("stage", "")
    elapsed = stock.get("elapsed_seconds", 0)
    completed_groups = stock.get("completed_groups", [])

    # Stage status icons
    stage_icons = {
        "analysts": "Analysts",
        "research": "Research",
        "trading": "Trading",
        "risk": "Risk",
        "portfolio": "Portfolio",
    }
    status_line = ""
    for group, label in stage_icons.items():
        if group in completed_groups:
            status_line += f"✅ {label} "
        elif status == "in_progress" and group == (stock.get("stage", "").lower().split()[0] if stock.get("stage") else ""):
            status_line += f"🔄 {label} "
        else:
            status_line += f"⏳ {label} "

    summary_lines = [
        f"📄 <b>{ticker} Report</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Status: {'✅ Completed' if status == 'completed' else '🔄 In Progress'}",
    ]
    if signal:
        summary_lines.append(f"Signal: {signal}")
    if elapsed:
        summary_lines.append(f"Time: {elapsed:.0f}s")
    summary_lines.append(f"\n{status_line}")

    _send(token, chat_id, "\n".join(summary_lines))

    # Generate and send report file
    report_file = _generate_report_file(ticker, stock)
    if report_file:
        from tradingagents.notifications.telegram import send_document
        caption = f"{ticker} — {'Complete' if status == 'completed' else f'Partial ({len(completed_groups)}/5 stages)'}"
        send_document(str(report_file), caption=caption)
        # Clean up temp file
        try:
            report_file.unlink()
        except Exception:
            pass


def _generate_report_file(ticker: str, stock: dict) -> Optional[Path]:
    """Generate a markdown report file from partial or complete state."""
    status = stock.get("status", "pending")
    today = _today()

    # Try to load partial state first (works for both complete and in-progress)
    partial_path = _results_dir() / "daily" / f"{ticker}_partial.json"
    partial_state = {}
    if partial_path.exists():
        try:
            with open(partial_path, encoding="utf-8") as f:
                partial_state = json.load(f)
        except Exception:
            pass

    # For completed stocks, also try loading the full report
    if status == "completed":
        report_dir = _results_dir() / "daily" / today / ticker
        complete_report = report_dir / "complete_report.md"
        if complete_report.exists():
            # Read and return as temp file
            content = complete_report.read_text(encoding="utf-8")
            tmp = Path(tempfile.mktemp(suffix=".md", prefix=f"{ticker}_"))
            tmp.write_text(content, encoding="utf-8")
            return tmp

    # Generate report from partial state
    if not partial_state:
        return None

    sections = []
    completed_groups = stock.get("completed_groups", [])

    # Header
    header = f"# Trading Analysis Report: {ticker}\n\n"
    header += f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    if status == "completed":
        header += "Status: Completed\n\n"
    else:
        header += f"Status: In Progress ({len(completed_groups)}/5 stages complete)\n\n"

    # I. Analyst Team Reports
    analyst_parts = []
    for name, key in [("Market Analyst", "market_report"),
                      ("Sentiment Analyst", "sentiment_report"),
                      ("News Analyst", "news_report"),
                      ("Fundamentals Analyst", "fundamentals_report")]:
        if partial_state.get(key):
            analyst_parts.append((name, partial_state[key]))
    if analyst_parts:
        content = "\n\n".join(f"### {name}\n{text}" for name, text in analyst_parts)
        sections.append(f"## I. Analyst Team Reports\n\n{content}")

    # II. Research Team Decision
    debate = partial_state.get("investment_debate_state", {})
    if debate:
        research_parts = []
        if debate.get("bull_history"):
            research_parts.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            research_parts.append(("Bear Researcher", debate["bear_history"]))
        if debate.get("judge_decision"):
            research_parts.append(("Research Manager", debate["judge_decision"]))
        if research_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in research_parts)
            sections.append(f"## II. Research Team Decision\n\n{content}")

    # III. Trading Team Plan
    if partial_state.get("trader_investment_plan"):
        sections.append(f"## III. Trading Team Plan\n\n### Trader\n{partial_state['trader_investment_plan']}")

    # IV. Risk Management
    risk = partial_state.get("risk_debate_state", {})
    if risk:
        risk_parts = []
        if risk.get("aggressive_history"):
            risk_parts.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_parts.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_parts.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in risk_parts)
            sections.append(f"## IV. Risk Management Team Decision\n\n{content}")

        if risk.get("judge_decision"):
            sections.append(f"## V. Portfolio Manager Decision\n\n### Portfolio Manager\n{risk['judge_decision']}")

    if not sections:
        return None

    report_content = header + "\n\n".join(sections)
    tmp = Path(tempfile.mktemp(suffix=".md", prefix=f"{ticker}_"))
    tmp.write_text(report_content, encoding="utf-8")
    return tmp


def _handle_callback(token: str, chat_id: str, callback_query: dict) -> None:
    """Handle inline keyboard button taps."""
    from tradingagents.notifications.telegram import answer_callback

    query_id = callback_query["id"]
    data = callback_query.get("data", "")

    # Answer callback to dismiss loading spinner
    answer_callback(query_id)

    if data.startswith("report:"):
        ticker = data.split(":", 1)[1]
        progress = _load_progress(_today())
        stocks = _get_stock_list(progress)

        # Find the stock
        matched = None
        for s in stocks:
            if s["ticker"] == ticker:
                matched = s
                break

        if matched:
            _send_stock_report(token, chat_id, matched)
        else:
            _send(token, chat_id, f"❌ {ticker} not found in today's data.")


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

    # Source .env, set PYTHONPATH, and run batch runner directly (like cron-wrapper.sh does)
    subprocess.Popen(
        f"source {project_dir}/.env && cd {project_dir} && "
        f"PYTHONPATH={project_dir}:$PYTHONPATH "
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
    parts = command.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        text = _cmd_start()
    elif cmd == "/status":
        text = _cmd_status()
    elif cmd == "/report":
        _cmd_report(token, chat_id, args)
        return
    elif cmd == "/signals":
        text = _cmd_signals()
    elif cmd == "/errors":
        text = _cmd_errors()
    elif cmd == "/last":
        text = _cmd_last()
    elif cmd == "/history":
        text = _cmd_history()
    elif cmd == "/run":
        text = _cmd_run()
        _send(token, chat_id, text)
        return
    else:
        text = f"Unknown command: {cmd}\n\nType /help for available commands."

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

                # Handle callback queries (inline keyboard button taps)
                if "callback_query" in update:
                    callback = update["callback_query"]
                    sender_chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
                    if sender_chat_id == chat_id:
                        logger.info("Callback from %s: %s", sender_chat_id, callback.get("data"))
                        _handle_callback(token, chat_id, callback)
                    continue

                # Handle regular messages
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
