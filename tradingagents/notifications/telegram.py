"""Telegram notifications for TradingAgents.

Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
"""

from __future__ import annotations

import os
import logging
import traceback
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.telegram.org/bot{token}"


def _get_config():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    return token, chat_id


def _send(message: str, parse_mode: str = "HTML") -> bool:
    token, chat_id = _get_config()
    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping notification")
        return False

    url = f"{_BASE_URL.format(token=token)}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": parse_mode},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error("Telegram API error %s: %s", resp.status_code, resp.text)
            return False
        return True
    except Exception as exc:
        logger.error("Failed to send Telegram message: %s", exc)
        return False


def send_alert(
    ticker: str,
    rating: str,
    price_target: Optional[float] = None,
    entry_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    time_horizon: Optional[str] = None,
    summary: str = "",
) -> bool:
    """Send a buy/overweight signal alert."""
    emoji = "🟢" if rating.lower() == "buy" else "🔵"
    lines = [
        f"{emoji} <b>{rating.upper()} Signal: {ticker}</b>",
        "━" * 24,
    ]

    if price_target is not None:
        lines.append(f"Price Target: ₹{price_target:,.2f}")

    levels = []
    if entry_price is not None:
        levels.append(f"Entry: ₹{entry_price:,.2f}")
    if stop_loss is not None:
        levels.append(f"Stop Loss: ₹{stop_loss:,.2f}")
    if levels:
        lines.append(" | ".join(levels))

    if time_horizon:
        lines.append(f"Time Horizon: {time_horizon}")

    if summary:
        lines.append("")
        truncated = summary[:400] + "..." if len(summary) > 400 else summary
        lines.append(truncated)

    lines.append("")
    lines.append(f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')}</i>")

    return _send("\n".join(lines))


def send_error(message: str, level: str = "warning") -> bool:
    """Send an error notification.

    Levels: info, warning, critical
    """
    emojis = {"info": "ℹ️", "warning": "🟡", "critical": "🔴"}
    emoji = emojis.get(level, "⚠️")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    text = (
        f"{emoji} <b>{level.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{message}\n\n"
        f"<i>{timestamp}</i>"
    )
    return _send(text)


def send_daily_summary(
    stocks_analyzed: int,
    buy_signals: Optional[list[dict]] = None,
    errors: Optional[list[str]] = None,
) -> bool:
    """Send end-of-day scan summary."""
    lines = [
        "📊 <b>Daily Scan Complete</b>",
        "━" * 24,
        f"Stocks Analyzed: {stocks_analyzed}",
    ]

    if buy_signals:
        lines.append(f"Buy Signals Found: {len(buy_signals)}")
        for sig in buy_signals:
            ticker = sig.get("ticker", "?")
            rating = sig.get("rating", "?")
            pt = sig.get("price_target")
            pt_str = f" | Target: ₹{pt:,.2f}" if pt else ""
            lines.append(f"  • <b>{ticker}</b> — {rating}{pt_str}")
    else:
        lines.append("Buy Signals Found: 0")

    if errors:
        lines.append(f"\nErrors: {len(errors)}")
        for err in errors[:5]:
            lines.append(f"  • {err}")
        if len(errors) > 5:
            lines.append(f"  ... and {len(errors) - 5} more")

    lines.append("")
    lines.append(f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')}</i>")

    return _send("\n".join(lines))
