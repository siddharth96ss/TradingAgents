"""Tests for batch_runner ticker normalisation and Indian suffix handling."""

from __future__ import annotations

import unittest

from tradingagents.batch_runner import _ensure_indian_suffix


class EnsureIndianSuffixTests(unittest.TestCase):
    """_ensure_indian_suffix should append .NS to bare Indian tickers."""

    def test_bare_nse_ticker_gets_ns(self):
        self.assertEqual(_ensure_indian_suffix("RELIANCE"), "RELIANCE.NS")
        self.assertEqual(_ensure_indian_suffix("TCS"), "TCS.NS")
        self.assertEqual(_ensure_indian_suffix("INFY"), "INFY.NS")
        self.assertEqual(_ensure_indian_suffix("HDFCBANK"), "HDFCBANK.NS")

    def test_already_has_ns_unchanged(self):
        self.assertEqual(_ensure_indian_suffix("RELIANCE.NS"), "RELIANCE.NS")
        self.assertEqual(_ensure_indian_suffix("TCS.NS"), "TCS.NS")

    def test_already_has_bo_unchanged(self):
        self.assertEqual(_ensure_indian_suffix("RELIANCE.BO"), "RELIANCE.BO")
        self.assertEqual(_ensure_indian_suffix("TCS.BO"), "TCS.BO")

    def test_already_has_nse_unchanged(self):
        self.assertEqual(_ensure_indian_suffix("RELIANCE.NSE"), "RELIANCE.NSE")

    def test_case_insensitive_check(self):
        # Even if the ticker comes in lowercase, the suffix check should work
        self.assertEqual(_ensure_indian_suffix("reliance"), "reliance.NS")
        self.assertEqual(_ensure_indian_suffix("reliance.ns"), "reliance.ns")

    def test_us_ticker_also_gets_ns(self):
        # The function doesn't distinguish — it assumes all bare tickers are
        # Indian because the scanner only scans Indian universes.
        self.assertEqual(_ensure_indian_suffix("NVDA"), "NVDA.NS")
        self.assertEqual(_ensure_indian_suffix("AAPL"), "AAPL.NS")

    def test_ticker_with_hyphen(self):
        self.assertEqual(_ensure_indian_suffix("BAJAJ-AUTO"), "BAJAJ-AUTO.NS")

    def test_empty_string(self):
        self.assertEqual(_ensure_indian_suffix(""), ".NS")


if __name__ == "__main__":
    unittest.main()
