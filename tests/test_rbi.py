"""RBI macro vendor: alias resolution, Indian stock detection, output formatting,
live fetch handling, and router integration.

All external API calls are mocked, so these run without a network connection
or installed packages (jugaad_data, esankhyiki, yfinance).
"""
import copy
import unittest
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import interface, rbi
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.india_detection import is_indian_stock

# ── Stub data ────────────────────────────────────────────────────────────

_RBI_RATES = {
    "Policy Repo Rate": "6.50%",
    "Reverse Repo Rate": "3.35%",
    "CRR": "4.50%",
    "SLR": "18.00%",
    "Bank Rate": "6.75%",
    "Standing Deposit Facility (SDF) Rate": "6.25%",
    "Marginal Standing Facility (MSF) Rate": "6.75%",
}


# ── Alias resolution tests ──────────────────────────────────────────────

@pytest.mark.unit
class RbiAliasTests(unittest.TestCase):
    def test_known_aliases_resolve(self):
        self.assertEqual(rbi.INDIA_MACRO_SERIES["rbi_repo_rate"], "RBI_REPO_RATE")
        self.assertEqual(rbi.INDIA_MACRO_SERIES["repo_rate"], "RBI_REPO_RATE")
        self.assertEqual(rbi.INDIA_MACRO_SERIES["crr"], "CRR")
        self.assertEqual(rbi.INDIA_MACRO_SERIES["slr"], "SLR")
        self.assertEqual(rbi.INDIA_MACRO_SERIES["bank_rate"], "BANK_RATE")
        self.assertEqual(rbi.INDIA_MACRO_SERIES["india_cpi"], "INDIA_CPI")
        self.assertEqual(rbi.INDIA_MACRO_SERIES["inr_usd"], "INR_USD")
        self.assertEqual(rbi.INDIA_MACRO_SERIES["india_vix"], "INDIA_VIX")
        self.assertEqual(rbi.INDIA_MACRO_SERIES["fii_dii"], "FII_DII")

    def test_unknown_alias_returns_guidance(self):
        out = rbi.get_macro_data("bank of japan rate", "2026-01-01")
        self.assertIn("RBI", out)
        self.assertIn("not a known Indian macro alias", out)


# ── Indian stock detection tests ────────────────────────────────────────

@pytest.mark.unit
class IndiaDetectionTests(unittest.TestCase):
    def test_ns_suffix_detected(self):
        self.assertTrue(is_indian_stock("RELIANCE.NS"))
        self.assertTrue(is_indian_stock("TCS.NS"))
        self.assertTrue(is_indian_stock("infy.ns"))  # case-insensitive

    def test_bo_suffix_detected(self):
        self.assertTrue(is_indian_stock("RELIANCE.BO"))
        self.assertTrue(is_indian_stock("TCS.BO"))

    def test_non_indian_not_detected(self):
        self.assertFalse(is_indian_stock("NVDA"))
        self.assertFalse(is_indian_stock("AAPL"))
        self.assertFalse(is_indian_stock("0700.HK"))

    def test_empty_or_none_returns_false(self):
        self.assertFalse(is_indian_stock(""))
        self.assertFalse(is_indian_stock(None))


# ── RBI policy rate formatting tests ─────────────────────────────────────

@pytest.mark.unit
class RbiFormattingTests(unittest.TestCase):
    def test_repo_rate_format(self):
        with mock.patch.object(rbi, "_fetch_rbi_policy_rates", return_value=_RBI_RATES):
            out = rbi.get_macro_data("rbi_repo_rate", "2026-01-01")
        self.assertIn("## RBI: Policy Repo Rate", out)
        self.assertIn("6.50%", out)
        self.assertIn("Current Rate", out)

    def test_crr_format(self):
        with mock.patch.object(rbi, "_fetch_rbi_policy_rates", return_value=_RBI_RATES):
            out = rbi.get_macro_data("crr", "2026-01-01")
        self.assertIn("CRR", out)
        self.assertIn("4.50%", out)

    def test_all_rates_table_included(self):
        with mock.patch.object(rbi, "_fetch_rbi_policy_rates", return_value=_RBI_RATES):
            out = rbi.get_macro_data("rbi_repo_rate", "2026-01-01")
        self.assertIn("All RBI Policy Rates", out)
        self.assertIn("Policy Repo Rate", out)
        self.assertIn("Reverse Repo Rate", out)


# ── yfinance-based data tests ───────────────────────────────────────────

@pytest.mark.unit
class RbiYfinanceTests(unittest.TestCase):
    def test_inr_usd_format(self):
        # Stub yfinance Ticker
        mock_history = mock.MagicMock()
        mock_history.empty = False
        mock_history.__iter__ = mock.MagicMock(return_value=iter([
            (mock.MagicMock(strftime=mock.MagicMock(return_value="2025-06-01")), {"Close": 83.5}),
            (mock.MagicMock(strftime=mock.MagicMock(return_value="2025-09-01")), {"Close": 84.2}),
        ]))

        mock_ticker = mock.MagicMock()
        mock_ticker.info = {"symbol": "USDINR=X"}
        mock_ticker.history.return_value = mock_history

        with mock.patch("tradingagents.dataflows.rbi.requests"), \
             mock.patch("yfinance.Ticker", return_value=mock_ticker):
            out = rbi.get_macro_data("inr_usd", "2026-01-01", 365)
        self.assertIn("INR/USD", out)
        self.assertIn("Yahoo Finance", out)

    def test_india_vix_format(self):
        mock_history = mock.MagicMock()
        mock_history.empty = False
        mock_history.__iter__ = mock.MagicMock(return_value=iter([]))

        mock_ticker = mock.MagicMock()
        mock_ticker.info = {"symbol": "^INDIAVIX"}
        mock_ticker.history.return_value = mock_history

        with mock.patch("yfinance.Ticker", return_value=mock_ticker):
            out = rbi.get_macro_data("india_vix", "2026-01-01", 365)
        self.assertIn("India VIX", out)


# ── Error handling tests ─────────────────────────────────────────────────

@pytest.mark.unit
class RbiErrorTests(unittest.TestCase):
    def test_rbi_not_configured_error_is_value_error(self):
        """RbiNotConfiguredError should be a ValueError for routing compatibility."""
        self.assertTrue(issubclass(rbi.RbiNotConfiguredError, ValueError))

    def test_policy_rate_fetch_failure_returns_message(self):
        with mock.patch.object(
            rbi, "_fetch_rbi_policy_rates",
            side_effect=rbi.RbiNotConfiguredError("jugaad_data not installed"),
        ):
            out = rbi.get_macro_data("rbi_repo_rate", "2026-01-01")
        self.assertIn("RBI Policy Rates", out)
        self.assertIn("not installed", out)


# ── Router integration tests ─────────────────────────────────────────────

@pytest.mark.unit
class RbiRoutingTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_macro_category_includes_rbi(self):
        self.assertEqual(
            interface.get_category_for_method("get_macro_indicators"), "macro_data"
        )
        self.assertIn("rbi", interface.VENDOR_METHODS["get_macro_indicators"])

    def test_indian_ticker_routes_to_rbi(self):
        set_config({"data_vendors": {"macro_data": "fred,rbi"}})
        with mock.patch.object(interface, "is_indian_stock", return_value=True), \
             mock.patch.dict(
                 interface.VENDOR_METHODS,
                 {
                     "get_macro_indicators": {
                         "fred": lambda *a, **k: "FRED_OK",
                         "rbi": lambda *a, **k: "RBI_OK",
                     }
                 },
                 clear=False,
             ):
            out = interface.route_to_vendor(
                "get_macro_indicators", "rbi_repo_rate", "2026-06-01", 365,
                ticker="RELIANCE.NS",
            )
        self.assertEqual(out, "RBI_OK")

    def test_global_indicator_still_uses_fred_for_indian_stock(self):
        set_config({"data_vendors": {"macro_data": "fred,rbi"}})
        with mock.patch.object(interface, "is_indian_stock", return_value=True), \
             mock.patch.dict(
                 interface.VENDOR_METHODS,
                 {
                     "get_macro_indicators": {
                         "fred": lambda *a, **k: "FRED_OK",
                         "rbi": lambda *a, **k: "RBI_OK",
                     }
                 },
                 clear=False,
             ):
            out = interface.route_to_vendor(
                "get_macro_indicators", "fed_funds_rate", "2026-06-01", 365,
                ticker="RELIANCE.NS",
            )
        # fed_funds_rate is NOT in INDIA_MACRO_SERIES, so it should go to FRED
        self.assertEqual(out, "FRED_OK")

    def test_non_indian_ticker_uses_fred(self):
        set_config({"data_vendors": {"macro_data": "fred,rbi"}})
        with mock.patch.object(interface, "is_indian_stock", return_value=False), \
             mock.patch.dict(
                 interface.VENDOR_METHODS,
                 {
                     "get_macro_indicators": {
                         "fred": lambda *a, **k: "FRED_OK",
                         "rbi": lambda *a, **k: "RBI_OK",
                     }
                 },
                 clear=False,
             ):
            out = interface.route_to_vendor(
                "get_macro_indicators", "cpi", "2026-06-01", 365,
                ticker="NVDA",
            )
        self.assertEqual(out, "FRED_OK")


if __name__ == "__main__":
    unittest.main()
