"""Groww.in news scraper for Indian stocks.

Uses the Groww News API (``/v1/api/groww-news/v2/stocks/news/{gsin}``) for
per-stock news, with a fallback to the server-rendered ``__NEXT_DATA__`` on
the general market news page.

Two entry points mirror the existing news interface:

* ``get_news(ticker, start_date, end_date)`` – per-stock Indian news via the
  Groww News API, matched by NSE script code.
* ``get_global_news(curr_date, look_back_days, limit)`` – recent Indian market
  headlines from the general Groww market news page.

No API key is required.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import urllib.request
from datetime import datetime, timezone

from dateutil.relativedelta import relativedelta

from .config import get_config
from .date_window import in_window

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_MAX_FEED_BYTES = 5 * 1024 * 1024  # 5 MB cap

# Groww News API: per-stock news by GSIN (Groww Stock Identifier).
# GSIN format is ``GSTK{BSE_CODE}`` (e.g. GSTK500325 for RELIANCE).
_GROWW_NEWS_API = "https://groww.in/v1/api/groww-news/v2/stocks/news/{}"

# General market news page (fallback + global news source).
_GROWW_NEWS_PAGE = "https://groww.in/market-news/stocks"


# ── Low-level helpers ────────────────────────────────────────────────────────


def _http_get_json(url: str) -> dict | None:
    """Fetch *url* and return parsed JSON, or ``None`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(_MAX_FEED_BYTES).decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Groww fetch failed for %s: %s", url, exc)
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Groww JSON decode failed for %s: %s", url, exc)
        return None


def _fetch_groww_page(url: str) -> dict | None:
    """Fetch a Groww page and extract the ``__NEXT_DATA__`` JSON."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(_MAX_FEED_BYTES).decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Groww page fetch failed for %s: %s", url, exc)
        return None

    match = re.search(
        r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        raw,
        re.DOTALL,
    )
    if not match:
        logger.warning("No __NEXT_DATA__ found on %s", url)
        return None

    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse Groww JSON from %s: %s", url, exc)
        return None


def _parse_timestamp(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to a UTC datetime."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


# ── NSE → BSE → GSIN mapping ────────────────────────────────────────────────


@functools.lru_cache(maxsize=1)
def _build_nse_to_gsin_map() -> dict[str, str]:
    """Build ``{NSE_CODE: GSIN}`` from Groww market pages.

    Sources combined (first match wins):
      1. Top gainers / losers pages — contain NSE codes, BSE codes, and GSINs
         for the NIFTY 100 constituents.
      2. General market news page — contains NSE/BSE pairs in article CTA metadata.

    The combined map covers 100+ popular Indian stocks.  Stocks not in the map
    fall back to the general news endpoint.
    """
    mapping: dict[str, str] = {}

    # Source 1: top gainers + losers (NIFTY 100 constituents with GSINs)
    for url in (
        "https://groww.in/markets/top-gainers?index=GIDXNIFTY100",
        "https://groww.in/markets/top-losers?index=GIDXNIFTY100",
    ):
        page = _fetch_groww_page(url)
        if page is None:
            continue
        stocks = page.get("props", {}).get("pageProps", {}).get("stocks", [])
        for s in stocks:
            nse = (s.get("nseScriptCode") or "").upper()
            gsin = s.get("gsin", "")
            if nse and gsin:
                mapping.setdefault(nse, gsin)

    # Source 2: general news page (additional NSE→BSE pairs)
    news_page = _fetch_groww_page(_GROWW_NEWS_PAGE)
    if news_page is not None:
        news_items = (
            news_page.get("props", {}).get("pageProps", {}).get("data", {}).get("news", [])
        )
        for item in news_items:
            for cta in item.get("data", {}).get("cta", []):
                meta = cta.get("meta", {})
                nse = (meta.get("nseScriptCode") or "").upper()
                bse = meta.get("bseScriptCode", "")
                if nse and bse:
                    mapping.setdefault(nse, f"GSTK{bse}")

    logger.debug("Built Groww NSE→GSIN map with %d entries", len(mapping))
    return mapping


def _resolve_gsin(ticker: str) -> str | None:
    """Resolve a ticker (e.g. ``RELIANCE.NS``) to a Groww GSIN.

    Strips the exchange suffix, upper-cases, and looks up in the cached map.
    Returns ``None`` when the stock isn't in the map.
    """
    raw_code = ticker.upper().split(".")[0] if "." in ticker else ticker.upper()
    nse_to_gsin = _build_nse_to_gsin_map()
    return nse_to_gsin.get(raw_code)


# ── Per-stock news (Groww News API) ─────────────────────────────────────────


def _fetch_stock_news_api(gsin: str) -> list[dict]:
    """Fetch per-stock news from the Groww News API."""
    data = _http_get_json(_GROWW_NEWS_API.format(gsin))
    if data is None:
        return []
    return data.get("results", [])


def _format_api_item(item: dict) -> tuple[str, str, str, datetime | None]:
    """Return ``(title, summary, url, pub_dt)`` from an API news item."""
    title = item.get("title", "Untitled")
    summary = item.get("summary", "")
    url = item.get("url", "")
    pub_dt = _parse_timestamp(item.get("pubDate"))
    return title, summary, url, pub_dt


def get_news(
    ticker: str,
    start_date: str,
    end_date: str,
) -> str:
    """Fetch Indian stock news from Groww for *ticker*.

    Strategy:
      1. Resolve NSE code → GSIN via the cached mapping.
      2. Call the Groww News API for per-stock news.
      3. Fall back to the general market news page if the GSIN isn't available.

    Returns a formatted plaintext block identical in style to the other
    news vendors so the downstream agents don't need source-specific logic.
    """
    config = get_config()
    article_limit = config.get("news_article_limit", 20)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # ── Strategy 1: per-stock API ───────────────────────────────────────
    gsin = _resolve_gsin(ticker)
    if gsin:
        try:
            raw_items = _fetch_stock_news_api(gsin)
        except Exception as exc:
            logger.warning("Groww News API failed for %s: %s", gsin, exc)
            raw_items = []
        matched: list[dict] = []
        for item in raw_items:
            _title, _summary, _url, pub_dt = _format_api_item(item)
            if in_window(pub_dt, start_dt, end_dt):
                matched.append(item)
            if len(matched) >= article_limit:
                break

        if matched:
            return _format_api_news(ticker, matched, start_date, end_date)

    # ── Strategy 2: general market news page (fallback) ─────────────────
    raw_code = ticker.upper().split(".")[0] if "." in ticker else ticker.upper()
    return _fallback_general_news(raw_code, ticker, start_dt, end_dt, article_limit)


def _format_api_news(ticker: str, items: list[dict], start_date: str, end_date: str) -> str:
    """Format per-stock API news items."""
    news_str = ""
    for item in items:
        title, summary, url, _pub_dt = _format_api_item(item)
        source = item.get("source", "Groww")
        news_str += f"### {title} (source: {source})\n"
        if summary:
            news_str += f"{summary}\n"
        if url:
            news_str += f"Link: {url}\n"
        news_str += "\n"
    return f"## {ticker} Groww News, from {start_date} to {end_date}:\n\n{news_str}"


def _fallback_general_news(
    raw_code: str, ticker: str, start_dt: datetime, end_dt: datetime, limit: int
) -> str:
    """Fall back to the general market news page, filtered by ticker."""
    page = _fetch_groww_page(_GROWW_NEWS_PAGE)
    if page is None:
        return f"Error fetching Groww news for {ticker}"

    news_items = (
        page.get("props", {}).get("pageProps", {}).get("data", {}).get("news", [])
    )
    if not news_items:
        return f"No Groww news found for {ticker}"

    matched: list[dict] = []
    for item in news_items:
        if not in_window(_parse_timestamp(item.get("publishedAt")), start_dt, end_dt):
            continue
        cta_list = item.get("data", {}).get("cta", [])
        for cta in cta_list:
            meta = cta.get("meta", {})
            code = (meta.get("nseScriptCode") or meta.get("bseScriptCode") or "").upper()
            if code == raw_code:
                matched.append(item)
                break
        if len(matched) >= limit:
            break

    if not matched:
        return (
            f"No Groww news found for {ticker} between "
            f"{start_dt.strftime('%Y-%m-%d')} and {end_dt.strftime('%Y-%m-%d')}. "
            "The stock may not be covered on Groww."
        )

    news_str = ""
    for item in matched:
        data = item.get("data", {})
        title = data.get("title", item.get("name", "Untitled"))
        body = data.get("body", "")
        source = item.get("publisher", "Groww")
        news_str += f"### {title} (source: {source})\n"
        if body:
            news_str += f"{body}\n"
        news_str += "\n"

    return f"## {ticker} Groww News, from {start_dt.strftime('%Y-%m-%d')} to {end_dt.strftime('%Y-%m-%d')}:\n\n{news_str}"


# ── Global / macro news ─────────────────────────────────────────────────────


def get_global_news(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Fetch recent Indian market headlines from Groww.

    Returns the most recent general market news items (all stocks) to give the
    agents an overview of what's happening on Indian markets.
    """
    config = get_config()
    if look_back_days is None:
        look_back_days = config.get("global_news_lookback_days", 7)
    if limit is None:
        limit = config.get("global_news_article_limit", 10)

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = curr_dt - relativedelta(days=look_back_days)

    page = _fetch_groww_page(_GROWW_NEWS_PAGE)
    if page is None:
        return "Error fetching Groww global news"

    news_items = (
        page.get("props", {}).get("pageProps", {}).get("data", {}).get("news", [])
    )
    if not news_items:
        return f"No Groww global news found for {curr_date}"

    news_str = ""
    kept = 0
    for item in news_items:
        if kept >= limit:
            break
        pub_dt = _parse_timestamp(item.get("publishedAt"))
        if not in_window(pub_dt, start_dt, curr_dt):
            continue

        data = item.get("data", {})
        title = data.get("title", item.get("name", "Untitled"))
        body = data.get("body", "")
        source = item.get("publisher", "Groww")
        news_str += f"### {title} (source: {source})\n"
        if body:
            news_str += f"{body}\n"
        news_str += "\n"
        kept += 1

    if kept == 0:
        start_date = start_dt.strftime("%Y-%m-%d")
        return f"No Groww global news found between {start_date} and {curr_date}"

    start_date = start_dt.strftime("%Y-%m-%d")
    return f"## Groww Indian Market News, from {start_date} to {curr_date}:\n\n{news_str}"
