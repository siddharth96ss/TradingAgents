"""Detect whether a ticker represents an Indian-listed stock.

Uses a two-tier approach:
  1. Fast path: check for .NS (NSE) or .BO (BSE) suffix — no API call.
  2. Slow path: check the cached yfinance exchange metadata via
     ``resolve_instrument_identity`` — catches bare Indian tickers like
     ``RELIANCE`` without a suffix.

Both paths are zero-cost after the first call per ticker because
``resolve_instrument_identity`` is ``@lru_cache``d.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Yahoo Finance exchange identifiers for Indian exchanges.
_INDIAN_EXCHANGES = frozenset({"NSI", "BSE", "NSE", "BSE_IN"})


def is_indian_stock(ticker: str) -> bool:
    """Return True if *ticker* is listed on an Indian exchange (NSE or BSE).

    Resolution order (first match wins):
      1. Ticker ends with ``.NS`` or ``.BO`` → Indian (fast, no API).
      2. Check the cached yfinance ``exchange`` field from
         ``resolve_instrument_identity`` → Indian if exchange matches.
      3. Otherwise → not Indian (fail-open for non-Indian tickers).

    The second lookup is cached (``@lru_cache(maxsize=256)``), so the first
    call per ticker pays one yfinance ``.info`` lookup and every subsequent
    call is free.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        return False

    normalized = ticker.strip().upper()

    # Fast path: explicit exchange suffix.
    if normalized.endswith((".NS", ".BO")):
        return True

    # Slow path: check yfinance exchange metadata (cached).
    try:
        from tradingagents.agents.utils.agent_utils import resolve_instrument_identity

        identity = resolve_instrument_identity(ticker)
        exchange = (identity.get("exchange") or "").upper()
        if any(ex in exchange for ex in _INDIAN_EXCHANGES):
            logger.debug("Detected Indian stock %r via exchange field: %s", ticker, exchange)
            return True
    except Exception:  # noqa: BLE001 — fail-open, never block the run
        pass

    return False
