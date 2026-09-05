"""Indian macro data vendor.

Fetches Indian macroeconomic time series — RBI policy rates, inflation,
growth, forex, and market data — from live government sources. Used by the
news analyst to ground macro commentary for Indian stocks in actual Indian
data rather than US-centric FRED data.

Data sources (all live, no static caching):
  - RBI policy rates: jugaad_data (scrapes RBI website)
  - CPI, WPI, GDP, Unemployment: esankhyiki (MOSPI API)
  - INR/USD, India VIX, G-Sec yields: yfinance
  - FII/DII flows: NSE public JSON endpoints
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
DEFAULT_LOOKBACK_DAYS = 365
MAX_ROWS = 40

# ── Indian macro series aliases ──────────────────────────────────────────
# Maps friendly aliases to an internal key. Anything not listed is rejected
# with guidance, same as FRED's approach.
INDIA_MACRO_SERIES = {
    # RBI policy rates (source: jugaad_data → RBI website)
    "rbi_repo_rate": "RBI_REPO_RATE",
    "repo_rate": "RBI_REPO_RATE",
    "reverse_repo_rate": "REVERSE_REPO_RATE",
    "crr": "CRR",
    "slr": "SLR",
    "bank_rate": "BANK_RATE",
    "sdf_rate": "SDF_RATE",
    "msf_rate": "MSF_RATE",
    # Inflation (source: esankhyiki → MOSPI API)
    "india_cpi": "INDIA_CPI",
    "india_wpi": "INDIA_WPI",
    # Growth & output (source: esankhyiki → MOSPI API)
    "india_gdp": "INDIA_GDP",
    "india_iip": "INDIA_IIP",
    # Labor (source: esankhyiki → MOSPI API)
    "india_unemployment": "INDIA_UNEMPLOYMENT",
    # Market data (source: yfinance)
    "inr_usd": "INR_USD",
    "india_vix": "INDIA_VIX",
    "india_gsec_10y": "INDIA_GSEC_10Y",
    "india_gsec_5y": "INDIA_GSEC_5Y",
    "india_gsec_2y": "INDIA_GSEC_2Y",
    # Capital flows (source: NSE public JSON)
    "fii_dii": "FII_DII",
    "fii_flows": "FII_FLOWS",
    "dii_flows": "DII_FLOWS",
    # Forex (source: RBI / yfinance)
    "forex_reserves": "FOREX_RESERVES",
    "inr_eur": "INR_EUR",
    "inr_gbp": "INR_GBP",
    "inr_jpy": "INR_JPY",
}


class RbiNotConfiguredError(ValueError):
    """Raised when an RBI data source is unavailable.

    Subclass of ValueError so the routing layer's "vendor unavailable"
    handling keeps working.
    """


# ── RBI policy rates via jugaad_data ─────────────────────────────────────

def _fetch_rbi_policy_rates() -> dict:
    """Fetch current RBI policy rates via jugaad_data.

    Returns a dict like:
        {"Policy Repo Rate": "6.50%", "Reverse Repo Rate": "3.35%", ...}
    """
    try:
        from jugaad_data.rbi import RBI

        r = RBI()
        rates = r.current_rates()
        return rates
    except ImportError as exc:
        raise RbiNotConfiguredError(
            "jugaad_data package not installed. Install with: pip install jugaad-data"
        ) from exc
    except Exception as exc:
        raise RbiNotConfiguredError(f"Failed to fetch RBI policy rates: {exc}") from exc


def _parse_rate_value(raw: str) -> float | None:
    """Parse a rate string like '6.50%' or '6.50' to float."""
    if raw is None:
        return None
    cleaned = str(raw).strip().rstrip("%")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _format_rbi_rates(rates: dict, indicator_key: str) -> str:
    """Format RBI policy rates as a markdown report."""
    # Map internal keys to dict keys from jugaad_data
    key_map = {
        "RBI_REPO_RATE": "Policy Repo Rate",
        "REVERSE_REPO_RATE": "Reverse Repo Rate",
        "CRR": "CRR",
        "SLR": "SLR",
        "BANK_RATE": "Bank Rate",
        "SDF_RATE": "Standing Deposit Facility (SDF) Rate",
        "MSF_RATE": "Marginal Standing Facility (MSF) Rate",
    }

    rbi_key = key_map.get(indicator_key)
    display_name = rbi_key or indicator_key.replace("_", " ").title()
    value = rates.get(rbi_key) if rbi_key else None

    if value is None:
        # Try partial match
        for k, v in rates.items():
            if indicator_key.replace("_", " ").lower() in k.lower():
                value = v
                display_name = k
                break

    if value is None:
        return (
            f"## RBI: {display_name}\n"
            f"\nIndicator '{indicator_key}' not found in RBI current rates. "
            f"Available: {', '.join(rates.keys())}"
        )

    parsed = _parse_rate_value(value)
    value_str = f"{parsed:.2f}%" if parsed is not None else str(value)

    header = (
        f"## RBI: {display_name}\n"
        f"- Source: Reserve Bank of India (via jugaad_data)\n"
        f"- Units: %\n"
    )

    summary = f"\n**Current Rate:** {value_str}\n"

    # Show all rates for context
    table_rows = []
    for k, v in rates.items():
        parsed_v = _parse_rate_value(v)
        table_rows.append(f"| {k} | {parsed_v:.2f}% |" if parsed_v is not None else f"| {k} | {v} |")

    table = (
        "\n### All RBI Policy Rates\n"
        "\n| Rate | Value |\n| --- | --- |\n"
        + "\n".join(table_rows)
        + "\n"
    )

    return header + summary + table


# ── MOSPI data via esankhyiki ────────────────────────────────────────────

def _fetch_mospi_data(dataset: str, filters: dict) -> dict:
    """Fetch data from MOSPI via esankhyiki."""
    try:
        import esankhyiki
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = esankhyiki.get_data(dataset, filters, format="dict")
        return result
    except ImportError as exc:
        raise RbiNotConfiguredError(
            "esankhyiki package not installed. Install with: pip install mospi-esankhyiki"
        ) from exc
    except Exception as exc:
        raise RbiNotConfiguredError(f"Failed to fetch MOSPI data ({dataset}): {exc}") from exc


def _format_mospi_series(
    data: dict, title: str, source: str, units: str = ""
) -> str:
    """Format MOSPI time series data as a markdown report."""
    header = (
        f"## {title}\n"
        f"- Source: {source}\n"
        f"- Units: {units}\n" if units else f"## {title}\n- Source: {source}\n"
    )

    # Extract data points from the response
    records = []
    if isinstance(data, dict):
        # Try common response structures
        inner = data.get("data", data.get("records", data))
        if isinstance(inner, list):
            records = inner
        elif isinstance(inner, dict):
            # Flatten dict of lists
            for _k, v in inner.items():
                if isinstance(v, list):
                    records.extend(v)

    if not records:
        return header + "\nNo data returned from MOSPI.\n"

    # Try to extract date/value pairs
    points = []
    for record in records:
        if isinstance(record, dict):
            date_val = record.get("date") or record.get("Year") or record.get("year")
            value_val = (
                record.get("value")
                or record.get("Index")
                or record.get("index")
                or record.get("Inflation")
                or record.get("inflation")
            )
            if date_val is not None and value_val is not None:
                points.append((str(date_val), str(value_val)))

    if not points:
        return header + "\nData available but could not parse date/value pairs.\n"

    first_date, first_val = points[0]
    last_date, last_val = points[-1]

    summary = f"\n**Latest:** {last_val} ({last_date})\n"

    shown = points[-MAX_ROWS:]
    note = ""
    if len(points) > MAX_ROWS:
        note = f"\n_(showing the most recent {MAX_ROWS} of {len(points)} observations)_\n"

    table = (
        "\n| Date | Value |\n| --- | --- |\n"
        + "\n".join(f"| {d} | {v} |" for d, v in shown)
        + "\n"
    )

    return header + summary + note + table


# ── yfinance-based data ─────────────────────────────────────────────────

def _fetch_yfinance_data(symbol: str, period: str = "1y") -> dict:
    """Fetch data from yfinance. Returns ticker info/history."""
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        return {"info": ticker.info or {}, "history": ticker.history(period=period)}
    except Exception as exc:
        raise RbiNotConfiguredError(f"Failed to fetch yfinance data for {symbol}: {exc}") from exc


def _format_yfinance_series(
    symbol: str, title: str, units: str = "", period: str = "1y"
) -> str:
    """Fetch and format a yfinance time series as markdown."""
    try:
        data = _fetch_yfinance_data(symbol, period=period)
    except RbiNotConfiguredError as e:
        return f"## {title}\n\n{e}\n"

    history = data.get("history")
    if history is None or history.empty:
        return f"## {title}\n\nNo data available for {symbol}.\n"

    header = (
        f"## {title}\n"
        f"- Source: Yahoo Finance ({symbol})\n"
        f"- Units: {units}\n" if units else f"## {title}\n- Source: Yahoo Finance ({symbol})\n"
    )

    points = []
    for date_idx, row in history.iterrows():
        date_str = date_idx.strftime("%Y-%m-%d")
        close = row.get("Close")
        if close is not None:
            points.append((date_str, f"{float(close):.4f}"))

    if not points:
        return header + "\nNo observations available.\n"

    first_date, first_val = points[0]
    last_date, last_val = points[-1]

    try:
        delta = float(last_val) - float(first_val)
        base = float(first_val)
        pct = f" ({delta / base * 100:+.2f}%)" if base != 0 else ""
        summary = (
            f"\n**Latest:** {last_val} ({last_date}) | "
            f"**Change over window:** {delta:+.4f}{pct} "
            f"from {first_val} ({first_date})\n"
        )
    except ValueError:
        summary = f"\n**Latest:** {last_val} ({last_date})\n"

    shown = points[-MAX_ROWS:]
    note = ""
    if len(points) > MAX_ROWS:
        note = f"\n_(showing the most recent {MAX_ROWS} of {len(points)} observations)_\n"

    table = (
        "\n| Date | Close |\n| --- | --- |\n"
        + "\n".join(f"| {d} | {v} |" for d, v in shown)
        + "\n"
    )

    return header + summary + note + table


# ── NSE FII/DII data ────────────────────────────────────────────────────

def _fetch_nse_fii_dii() -> dict:
    """Fetch FII/DII flow data from NSE public endpoint."""
    url = "https://www.nseindia.com/api/fiidiiTradeReact"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    try:
        # NSE requires a session cookie first
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=REQUEST_TIMEOUT)
        response = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise RbiNotConfiguredError(f"Failed to fetch FII/DII data from NSE: {exc}") from exc


def _format_fii_dii(data: dict) -> str:
    """Format FII/DII data as markdown."""
    header = (
        "## FII/DII Flows\n"
        "- Source: National Stock Exchange of India\n"
        "- Units: INR Crores\n"
    )

    if not data:
        return header + "\nNo FII/DII data available.\n"

    # NSE returns a list of dicts or a dict with key "data"
    records = data if isinstance(data, list) else data.get("data", [])

    if not records:
        return header + "\nNo FII/DII flow records found.\n"

    table_rows = []
    for record in records[:10]:  # Last 10 trading days
        if isinstance(record, dict):
            date = record.get("date") or record.get("Dt", "")
            fii = record.get("fii") or record.get("FII", "N/A")
            dii = record.get("dii") or record.get("DII", "N/A")
            table_rows.append(f"| {date} | {fii} | {dii} |")

    if not table_rows:
        return header + "\nData available but could not parse records.\n"

    table = (
        "\n| Date | FII (INR Cr) | DII (INR Cr) |\n| --- | --- | --- |\n"
        + "\n".join(table_rows)
        + "\n"
    )

    return header + table


# ── Main entry point ─────────────────────────────────────────────────────

def get_macro_data(
    indicator: str,
    curr_date: str,
    look_back_days: int | None = None,
) -> str:
    """Fetch an Indian macroeconomic series as a formatted markdown report.

    This function mirrors the interface of ``fred.get_macro_data`` so it can
    be used as a drop-in replacement in the vendor routing layer.

    Args:
        indicator: A friendly alias (e.g. "rbi_repo_rate", "india_cpi",
            "inr_usd") or an internal key (e.g. "RBI_REPO_RATE").
        curr_date: The as-of date (yyyy-mm-dd). Bounds the observation window.
        look_back_days: Trailing window length; ``None`` uses DEFAULT_LOOKBACK_DAYS.

    Returns:
        A markdown report with the series title, source, latest value, and
        a recent observation table.
    """
    if look_back_days is None:
        look_back_days = DEFAULT_LOOKBACK_DAYS

    # Resolve alias to internal key
    key = indicator.strip().lower().replace(" ", "_").replace("-", "_")
    if key in INDIA_MACRO_SERIES:
        internal_key = INDIA_MACRO_SERIES[key]
    else:
        return (
            f"RBI: '{indicator}' is not a known Indian macro alias. "
            f"Known aliases: {', '.join(sorted(INDIA_MACRO_SERIES.keys()))}"
        )

    # ── Route to the appropriate data source ──

    # 1. RBI policy rates (jugaad_data)
    if internal_key in (
        "RBI_REPO_RATE", "REVERSE_REPO_RATE", "CRR", "SLR",
        "BANK_RATE", "SDF_RATE", "MSF_RATE",
    ):
        try:
            rates = _fetch_rbi_policy_rates()
            return _format_rbi_rates(rates, internal_key)
        except RbiNotConfiguredError as e:
            return f"## RBI Policy Rates\n\n{e}\n"

    # 2. MOSPI data (esankhyiki)
    if internal_key == "INDIA_CPI":
        try:
            from datetime import datetime as _dt
            year = _dt.strptime(curr_date, "%Y-%m-%d").year
            data = _fetch_mospi_data("CPI", {
                "base_year": "2012",
                "year": str(year),
                "series": "Current",
            })
            return _format_mospi_series(data, "India CPI (Consumer Price Index)", "MOSPI", "Index (2012=100)")
        except RbiNotConfiguredError as e:
            return f"## India CPI\n\n{e}\n"

    if internal_key == "INDIA_WPI":
        try:
            from datetime import datetime as _dt
            year = _dt.strptime(curr_date, "%Y-%m-%d").year
            data = _fetch_mospi_data("WPI", {
                "base_year": "2011-12",
                "year": str(year),
            })
            return _format_mospi_series(data, "India WPI (Wholesale Price Index)", "MOSPI", "Index (2011-12=100)")
        except RbiNotConfiguredError as e:
            return f"## India WPI\n\n{e}\n"

    if internal_key == "INDIA_GDP":
        try:
            data = _fetch_mospi_data("NAS", {
                "indicator_code": 1,
                "base_year": "2022-23",
                "frequency_code": 1,
            })
            return _format_mospi_series(data, "India GDP (National Accounts Statistics)", "MOSPI", "INR Crores")
        except RbiNotConfiguredError as e:
            return f"## India GDP\n\n{e}\n"

    if internal_key == "INDIA_IIP":
        try:
            from datetime import datetime as _dt
            year = _dt.strptime(curr_date, "%Y-%m-%d").year
            data = _fetch_mospi_data("IIP", {
                "base_year": "2011-12",
                "frequency": "Monthly",
                "year": str(year),
            })
            return _format_mospi_series(data, "India IIP (Index of Industrial Production)", "MOSPI", "Index (2011-12=100)")
        except RbiNotConfiguredError as e:
            return f"## India IIP\n\n{e}\n"

    if internal_key == "INDIA_UNEMPLOYMENT":
        try:
            data = _fetch_mospi_data("PLFS", {
                "indicator_code": 1,
                "frequency_code": 1,
            })
            return _format_mospi_series(data, "India Unemployment Rate (PLFS)", "MOSPI", "%")
        except RbiNotConfiguredError as e:
            return f"## India Unemployment\n\n{e}\n"

    # 3. yfinance market data
    yfinance_map = {
        "INR_USD": ("USDINR=X", "INR/USD Exchange Rate", "INR per 1 USD"),
        "INDIA_VIX": ("^INDIAVIX", "India VIX", "Volatility Index"),
        "INDIA_GSEC_10Y": ("^NSEI", "India 10-Year G-Sec Yield (proxy via Nifty)", "%"),
        "INDIA_GSEC_5Y": ("^NSEI", "India 5-Year G-Sec Yield (proxy via Nifty)", "%"),
        "INDIA_GSEC_2Y": ("^NSEI", "India 2-Year G-Sec Yield (proxy via Nifty)", "%"),
        "INR_EUR": ("EURINR=X", "INR/EUR Exchange Rate", "INR per 1 EUR"),
        "INR_GBP": ("GBPINR=X", "INR/GBP Exchange Rate", "INR per 1 GBP"),
        "INR_JPY": ("JPYINR=X", "INR/JPY Exchange Rate", "INR per 1 JPY"),
    }

    if internal_key in yfinance_map:
        symbol, title, units = yfinance_map[internal_key]
        return _format_yfinance_series(symbol, title, units)

    # 4. NSE FII/DII data
    if internal_key in ("FII_DII", "FII_FLOWS", "DII_FLOWS"):
        try:
            data = _fetch_nse_fii_dii()
            return _format_fii_dii(data)
        except RbiNotConfiguredError as e:
            return f"## FII/DII Flows\n\n{e}\n"

    # 5. Forex reserves (RBI website - best effort)
    if internal_key == "FOREX_RESERVES":
        return (
            "## India Forex Reserves\n"
            "- Source: Reserve Bank of India (weekly publication)\n"
            "- Note: Forex reserves are published weekly by RBI. "
            "Check https://data.rbi.org.in for the latest figures.\n"
            "- Latest available data should be verified from RBI's website.\n"
        )

    return (
        f"RBI: Internal key '{internal_key}' is mapped but not yet implemented. "
        f"Please add a data fetcher for this indicator."
    )
