"""Sector momentum screener for Indian stocks.

Ranks sector ETFs by recent momentum, then picks stocks from top sectors
that have outperformed their sector ETF over the last 30 days.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)

# Sector ETFs available on Yahoo Finance (.NS suffix)
# Some BEES ETFs are delisted — only include ones that work
SECTOR_ETFS = {
    "BANKBEES.NS": "Nifty Bank",
    "ITBEES.NS": "Nifty IT",
    "PHARMABEES.NS": "Nifty Pharma",
    "AUTOBEES.NS": "Nifty Auto",
    "INFRABEES.NS": "Nifty Infra",
    "NIFTYBEES.NS": "Nifty 50",
    "JUNIORBEES.NS": "Nifty Next 50",
}

# Proxy tickers for sectors without working ETFs.
# We calculate the sector return as the average of these top stocks.
SECTOR_PROXIES = {
    "Nifty FMCG": {
        "proxy_tickers": ["ITC.NS", "HINDUNILVR.NS", "NESTLEIND.NS", "BRITANNIA.NS", "DABUR.NS"],
        "stocks": [
            "HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS", "BRITANNIA.NS",
            "DABUR.NS", "MARICO.NS", "COLPAL.NS", "EMAMILTD.NS",
            "GODREJCP.NS", "VSTIND.NS", "RADICO.NS", "UNITDSPR.NS",
        ],
    },
    "Nifty Metal": {
        "proxy_tickers": ["TATASTEEL.NS", "HINDALCO.NS", "JSWSTEEL.NS", "NMDC.NS"],
        "stocks": [
            "TATASTEEL.NS", "HINDALCO.NS", "JSWSTEEL.NS", "NMDC.NS",
            "VEDL.NS", "SAIL.NS", "JINDALSTEL.NS", "NATIONALUM.NS",
            "HINDZINC.NS", "COALINDIA.NS", "GRASIM.NS", "ADANIENT.NS",
        ],
    },
    "Nifty Realty": {
        "proxy_tickers": ["DLF.NS", "GODREJPROP.NS", "OBEROIRLTY.NS", "PRESTIGE.NS"],
        "stocks": [
            "DLF.NS", "GODREJPROP.NS", "OBEROIRLTY.NS", "PRESTIGE.NS",
            "BRIGADE.NS", "SOBHA.NS", "PHOENIXLTD.NS", "LODHA.NS",
            "MACROTECH.NS",
        ],
    },
    "Nifty Energy": {
        "proxy_tickers": ["RELIANCE.NS", "NTPC.NS", "POWERGRID.NS", "ONGC.NS"],
        "stocks": [
            "RELIANCE.NS", "NTPC.NS", "POWERGRID.NS", "ONGC.NS",
            "ADANIGREEN.NS", "TATAPOWER.NS", "NHPC.NS",
            "BPCL.NS", "HINDPETRO.NS", "COALINDIA.NS",
        ],
    },
    "Nifty Media": {
        "proxy_tickers": ["TATAELXSI.NS", "PVRINOX.NS", "NETWORK18.NS"],
        "stocks": [
            "TATAELXSI.NS", "PVRINOX.NS", "NETWORK18.NS", "TV18BRDCST.NS",
            "SUNTV.NS", "ZEE.NS", "JTEKTINDIA.NS",
        ],
    },
    "Nifty PSU Bank": {
        "proxy_tickers": ["SBIN.NS", "PNB.NS", "BANKBARODA.NS", "CANBK.NS"],
        "stocks": [
            "SBIN.NS", "PNB.NS", "BANKBARODA.NS", "CANBK.NS",
            "UNIONBANK.NS", "INDIANB.NS", "UCO.NS", "BANKINDIA.NS",
        ],
    },
}


def _get_price_change(ticker: str, days: int = 30) -> Optional[float]:
    """Get percentage price change over N days."""
    try:
        end = datetime.now()
        start = end - timedelta(days=days + 10)  # extra buffer for weekends
        hist = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                          end=end.strftime("%Y-%m-%d"), progress=False)
        if hist is None or len(hist) < 2:
            return None
        close = hist["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        return float((close.iloc[-1] / close.iloc[0] - 1) * 100)
    except Exception as exc:
        logger.debug("Could not get price for %s: %s", ticker, exc)
        return None


def _get_proxy_return(tickers: list[str], days: int) -> Optional[float]:
    """Calculate average return across a list of proxy tickers."""
    returns = []
    for t in tickers:
        ret = _get_price_change(t, days)
        if ret is not None:
            returns.append(ret)
    if not returns:
        return None
    return sum(returns) / len(returns)


def rank_sectors(lookback_short: int = 5, lookback_long: int = 20) -> list[dict]:
    """Rank sectors by combined momentum score.

    For ETF-based sectors, uses the ETF price change.
    For proxy-based sectors, uses the average of proxy tickers.

    Returns list of dicts sorted by score (descending).
    """
    results = []

    # ETF-based sectors
    for etf_ticker, etf_name in SECTOR_ETFS.items():
        ret_short = _get_price_change(etf_ticker, lookback_short)
        ret_long = _get_price_change(etf_ticker, lookback_long)
        if ret_short is None or ret_long is None:
            logger.warning("Skipping %s — no data", etf_name)
            continue
        score = ret_short * 0.6 + ret_long * 0.4
        results.append({
            "ticker": etf_ticker,
            "name": etf_name,
            "score": round(score, 2),
            "ret_5d": round(ret_short, 2),
            "ret_20d": round(ret_long, 2),
            "source": "etf",
        })

    # Proxy-based sectors
    for sector_name, sector_info in SECTOR_PROXIES.items():
        proxy_tickers = sector_info["proxy_tickers"]
        ret_short = _get_proxy_return(proxy_tickers, lookback_short)
        ret_long = _get_proxy_return(proxy_tickers, lookback_long)
        if ret_short is None or ret_long is None:
            logger.warning("Skipping %s — no proxy data", sector_name)
            continue
        score = ret_short * 0.6 + ret_long * 0.4
        results.append({
            "ticker": proxy_tickers[0],  # use first proxy as reference
            "name": sector_name,
            "score": round(score, 2),
            "ret_5d": round(ret_short, 2),
            "ret_20d": round(ret_long, 2),
            "source": "proxy",
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def get_outperforming_stocks(
    sector_ticker: str,
    stock_list: list[str],
    lookback_days: int = 30,
    min_outperformance: float = 2.0,
    is_proxy: bool = False,
    proxy_tickers: Optional[list[str]] = None,
) -> list[dict]:
    """Get stocks from a sector that outperformed the sector benchmark.

    For ETF sectors, compares against the ETF.
    For proxy sectors, compares against the average of proxy tickers.
    """
    if is_proxy and proxy_tickers:
        benchmark_ret = _get_proxy_return(proxy_tickers, lookback_days)
    else:
        benchmark_ret = _get_price_change(sector_ticker, lookback_days)

    if benchmark_ret is None:
        logger.warning("Could not get benchmark return for %s", sector_ticker)
        return []

    results = []
    for stock in stock_list:
        stock_ret = _get_price_change(stock, lookback_days)
        if stock_ret is None:
            continue
        outperformance = stock_ret - benchmark_ret
        if outperformance >= min_outperformance:
            results.append({
                "ticker": stock,
                "stock_ret": round(stock_ret, 2),
                "etf_ret": round(benchmark_ret, 2),
                "outperformance": round(outperformance, 2),
            })

    results.sort(key=lambda x: x["outperformance"], reverse=True)
    return results


def get_todays_stocks(
    top_sectors: int = 3,
    stocks_per_sector: int = 8,
    lookback_days: int = 30,
    min_outperformance: float = 2.0,
    exclude_tickers: Optional[list[str]] = None,
) -> list[str]:
    """Main entry point: get today's stock list for analysis.

    1. Rank all sectors by momentum (ETF or proxy-based)
    2. Pick top N sectors
    3. For each sector, find stocks that outperformed the benchmark
    4. Return union of top stocks across sectors
    """
    exclude = set(exclude_tickers or [])

    logger.info("Ranking sectors by momentum...")
    rankings = rank_sectors()
    if not rankings:
        logger.error("Could not rank any sectors")
        return []

    top = rankings[:top_sectors]
    logger.info("Top sectors: %s", [f"{r['name']} ({r['score']})" for r in top])

    all_tickers = []
    for sector in top:
        sector_name = sector["name"]
        is_proxy = sector.get("source") == "proxy"

        # Get stock list for this sector
        if is_proxy:
            sector_info = SECTOR_PROXIES.get(sector_name, {})
            stocks = sector_info.get("stocks", [])
            proxy_tickers = sector_info.get("proxy_tickers")
        else:
            etf = sector["ticker"]
            stocks = _get_stocks_for_etf(etf)
            proxy_tickers = None

        if not stocks:
            logger.warning("No stock list for %s", sector_name)
            continue

        logger.info("Finding outperformers in %s...", sector_name)
        outperformers = get_outperforming_stocks(
            sector["ticker"], stocks, lookback_days, min_outperformance,
            is_proxy=is_proxy, proxy_tickers=proxy_tickers,
        )

        for op in outperformers[:stocks_per_sector]:
            if op["ticker"] not in exclude:
                all_tickers.append(op["ticker"])

        logger.info(
            "  %s: %d outperformers found, %d selected",
            sector_name, len(outperformers), min(len(outperformers), stocks_per_sector),
        )

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for t in all_tickers:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    logger.info("Total stocks selected: %d", len(unique))
    return unique


def _get_stocks_for_etf(etf_ticker: str) -> list[str]:
    """Get stock list for an ETF-based sector."""
    ETF_STOCKS = {
        "BANKBEES.NS": [
            "HDFCBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS", "AXISBANK.NS",
            "SBIN.NS", "INDUSINDBK.NS", "BANDHANBNK.NS", "FEDERALBNK.NS",
            "IDFCFIRSTB.NS", "PNB.NS", "BANKBARODA.NS", "CANBK.NS",
        ],
        "ITBEES.NS": [
            "TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS",
            "TECHM.NS", "LTIM.NS", "MPHASIS.NS", "PERSISTENT.NS",
            "COFORGE.NS", "LTTS.NS", "CYIENT.NS", "KPITTECH.NS",
        ],
        "PHARMABEES.NS": [
            "SUNPHARMA.NS", "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS",
            "IPCALAB.NS", "ALKEM.NS", "TORNTPHARM.NS", "AUROPHARMA.NS",
            "LUPIN.NS", "ZYDUSLIFE.NS", "GLAND.NS", "GRANULES.NS",
        ],
        "AUTOBEES.NS": [
            "MARUTI.NS", "M&M.NS", "TATAMOTORS.NS", "BAJAJ-AUTO.NS",
            "HEROMOTOCO.NS", "EICHERMOT.NS", "TVSMOTOR.NS", "ASHOKLEY.NS",
            "MOTHERSON.NS", "BOSCHLTD.NS", "EXIDEIND.NS", "AMARARAJA.NS",
        ],
        "INFRABEES.NS": [
            "ADANIENT.NS", "ADANIPORTS.NS", "LICI.NS", "ITC.NS",
            "IOC.NS", "BPCL.NS", "GAIL.NS", "PETRONET.NS",
            "AIAENG.NS", "L&T.NS", "ABB.NS", "SIEMENS.NS",
        ],
        "NIFTYBEES.NS": [
            "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS",
            "ICICIBANK.NS", "HINDUNILVR.NS", "ITC.NS", "SBIN.NS",
            "BHARTIARTL.NS", "KOTAKBANK.NS", "LT.NS", "AXISBANK.NS",
        ],
        "JUNIORBEES.NS": [
            "TATAMOTORS.NS", "SUNPHARMA.NS", "BAJFINANCE.NS", "HCLTECH.NS",
            "WIPRO.NS", "TITAN.NS", "MARUTI.NS", "NTPC.NS",
            "TATASTEEL.NS", "TECHM.NS", "POWERGRID.NS", "ONGC.NS",
        ],
    }
    return ETF_STOCKS.get(etf_ticker, [])
