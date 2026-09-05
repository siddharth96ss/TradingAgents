"""Tests for the Groww.in news scraper.

Covers the GSIN resolution, per-stock API, general news fallback, date-window
filtering, and network-failure graceful degradation paths.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from tradingagents.dataflows import groww_news


# ── Fixtures ─────────────────────────────────────────────────────────────────

_FAKE_API_ITEM = {
    "id": "884013127597783936",
    "title": "Reliance Industries Q2 profit jumps 12%",
    "summary": "Reliance Industries reported a 12% YoY rise in net profit.",
    "url": "https://example.com/reliance-q2",
    "imageUrl": None,
    "pubDate": "2026-09-05T10:00:00",
    "source": "BSE",
}

_FAKE_API_ITEM_TCS = {
    "id": "999999",
    "title": "TCS wins $2B deal from European bank",
    "summary": "Tata Consultancy Services bagged a major deal.",
    "url": "https://example.com/tcs-deal",
    "imageUrl": None,
    "pubDate": "2026-09-05T08:30:00",
    "source": "NSE",
}

_FAKE_NEWS_PAGE_ITEM = {
    "name": "123456_RELIANCE INDUSTRIES-1-VariationA",
    "postId": "123456",
    "publisher": "BSE",
    "publishedAt": "2026-09-05T10:00:00",
    "data": {
        "cta": [
            {
                "type": "STOCK",
                "ctaText": "Reliance Industries",
                "meta": {
                    "bseScriptCode": "500325",
                    "nseScriptCode": "RELIANCE",
                },
            }
        ],
        "title": "Reliance general market news",
        "body": "General market news about Reliance.",
    },
}


def _make_api_response(items: list[dict]) -> dict:
    """Build a minimal Groww News API response."""
    return {"results": items}


def _make_page(news_items: list[dict]) -> dict:
    """Build a minimal __NEXT_DATA__ structure with the given news items."""
    return {
        "props": {
            "pageProps": {
                "data": {
                    "news": news_items,
                }
            }
        }
    }


def _patch_api(items: list[dict] | None):
    """Patch _fetch_stock_news_api to return *items*."""
    if items is None:
        return patch.object(groww_news, "_fetch_stock_news_api", side_effect=Exception("network"))
    return patch.object(groww_news, "_fetch_stock_news_api", return_value=items)


def _patch_gsin_mapping(mapping: dict | None = None):
    """Patch _resolve_gsin to return a known mapping or None."""
    if mapping is None:
        return patch.object(groww_news, "_resolve_gsin", return_value=None)
    return patch.object(groww_news, "_resolve_gsin", side_effect=lambda t: mapping.get(t.upper().split(".")[0]))


def _patch_page(page: dict | None):
    """Patch _fetch_groww_page to return *page*."""
    return patch.object(groww_news, "_fetch_groww_page", return_value=page)


# ── Unit: _parse_timestamp ──────────────────────────────────────────────────

@pytest.mark.unit
class TestParseTimestamp:
    def test_parses_naive_iso(self):
        dt = groww_news._parse_timestamp("2026-09-05T10:00:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_returns_none_for_none(self):
        assert groww_news._parse_timestamp(None) is None

    def test_returns_none_for_garbage(self):
        assert groww_news._parse_timestamp("not-a-date") is None


# ── Unit: _resolve_gsin ─────────────────────────────────────────────────────

@pytest.mark.unit
class TestResolveGsin:
    def test_strips_ns_suffix(self):
        mapping = {"RELIANCE": "GSTK500325"}
        with patch.object(groww_news, "_build_nse_to_gsin_map", return_value=mapping):
            assert groww_news._resolve_gsin("RELIANCE.NS") == "GSTK500325"

    def test_bare_ticker(self):
        mapping = {"TCS": "GSTK532540"}
        with patch.object(groww_news, "_build_nse_to_gsin_map", return_value=mapping):
            assert groww_news._resolve_gsin("TCS") == "GSTK532540"

    def test_returns_none_for_unknown(self):
        with patch.object(groww_news, "_build_nse_to_gsin_map", return_value={}):
            assert groww_news._resolve_gsin("FAKE") is None


# ── Integration: get_news via API ───────────────────────────────────────────

@pytest.mark.unit
class TestGetNewsAPI:
    def test_returns_api_news(self):
        """When GSIN resolves, use the Groww News API."""
        with _patch_gsin_mapping({"RELIANCE": "GSTK500325"}), \
             _patch_api([_FAKE_API_ITEM]):
            result = groww_news.get_news("RELIANCE.NS", "2026-09-01", "2026-09-06")
        assert "Reliance Industries Q2 profit jumps 12%" in result
        assert "Link: https://example.com/reliance-q2" in result

    def test_bare_ticker_matches(self):
        """User types RELIANCE without .NS suffix."""
        with _patch_gsin_mapping({"RELIANCE": "GSTK500325"}), \
             _patch_api([_FAKE_API_ITEM]):
            result = groww_news.get_news("RELIANCE", "2026-09-01", "2026-09-06")
        assert "Reliance Industries Q2 profit jumps 12%" in result

    def test_date_filtering(self):
        """Items outside the date window are excluded."""
        old_item = {**_FAKE_API_ITEM, "pubDate": "2026-08-01T10:00:00"}
        with _patch_gsin_mapping({"RELIANCE": "GSTK500325"}), \
             _patch_api([old_item]):
            result = groww_news.get_news("RELIANCE", "2026-09-01", "2026-09-06")
        assert "No Groww news found" in result

    def test_api_failure_falls_back(self):
        """API failure should fall back to general news page."""
        with _patch_gsin_mapping({"RELIANCE": "GSTK500325"}), \
             _patch_api(None), \
             _patch_page(_make_page([_FAKE_NEWS_PAGE_ITEM])):
            result = groww_news.get_news("RELIANCE.NS", "2026-09-01", "2026-09-06")
        assert "Reliance general market news" in result

    def test_unknown_ticker_uses_fallback(self):
        """Ticker not in GSIN mapping falls back to general news page."""
        with _patch_gsin_mapping(None), \
             _patch_page(_make_page([_FAKE_NEWS_PAGE_ITEM])):
            result = groww_news.get_news("RELIANCE.NS", "2026-09-01", "2026-09-06")
        assert "Reliance general market news" in result

    def test_no_match_anywhere(self):
        """Ticker not found in API or general news."""
        with _patch_gsin_mapping({"WIPRO": "GSTK507685"}), \
             _patch_api([]), \
             _patch_page(_make_page([])):
            result = groww_news.get_news("WIPRO", "2026-09-01", "2026-09-06")
        assert "No Groww news found" in result

    def test_empty_api_returns_message(self):
        with _patch_gsin_mapping({"RELIANCE": "GSTK500325"}), \
             _patch_api([]):
            result = groww_news.get_news("RELIANCE.NS", "2026-09-01", "2026-09-06")
        assert "No Groww news found" in result


# ── Integration: get_global_news ─────────────────────────────────────────────

@pytest.mark.unit
class TestGetGlobalNews:
    def test_returns_headlines(self):
        page = _make_page([_FAKE_NEWS_PAGE_ITEM])
        with _patch_page(page):
            result = groww_news.get_global_news("2026-09-06", look_back_days=7, limit=5)
        assert "Indian Market News" in result
        assert "Reliance general market news" in result

    def test_limit_respected(self):
        items = [_FAKE_NEWS_PAGE_ITEM, {**_FAKE_NEWS_PAGE_ITEM, "name": "TCS item", "data": {**_FAKE_NEWS_PAGE_ITEM["data"], "title": "TCS news"}}]
        page = _make_page(items)
        with _patch_page(page):
            result = groww_news.get_global_news("2026-09-06", look_back_days=7, limit=1)
        assert "Reliance" in result

    def test_fetch_failure_returns_error(self):
        with _patch_page(None):
            result = groww_news.get_global_news("2026-09-06")
        assert "Error" in result
