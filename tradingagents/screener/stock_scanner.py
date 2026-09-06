"""Multi-signal stock scanner for Indian markets.

Single source of truth for stock scanning, scoring, and filtering.
Combines gap detection, volume spikes, breakouts, support/resistance,
trend analysis, RSI, and cyclical signals into a weighted scoring system.

Ported from inspiration/indian-trading-agent and adapted to work
without database dependencies — all state is computed at runtime.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import yfinance as yf

logger = logging.getLogger(__name__)


# ── Default Signal Weights ─────────────────────────────────────────────
# Each signal adds or subtracts from a cumulative score.
# Ported from inspiration recommender.py DEFAULT_WEIGHTS.
DEFAULT_WEIGHTS = {
    "gap_up_filled": 1.5,
    "gap_down_filled": 1.5,
    "gap_up_open": -0.5,
    "gap_down_open": -0.5,
    "volume_bullish": 2.0,
    "volume_bearish": -2.0,
    "breakout_vol_confirmed": 3.0,
    "breakout_weak": 1.0,
    "near_support": 2.0,
    "near_resistance": -1.5,
    "breakdown_support": -2.5,
    "cyclical_bullish": 1.5,
    "cyclical_bearish": -1.5,
    "rsi_oversold": 1.5,
    "rsi_overbought": -1.0,
    "uptrend_strong": 1.0,
    "downtrend_strong": -1.0,
}


# ── Stock Universes ────────────────────────────────────────────────────

NIFTY_50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BPCL",
    "BHARTIARTL", "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK",
    "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK",
    "ITC", "INDUSINDBK", "INFY", "JSWSTEEL", "KOTAKBANK",
    "LT", "M&M", "MARUTI", "NESTLEIND", "NTPC",
    "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN",
    "SUNPHARMA", "TCS", "TATACONSUM", "TATAMOTORS", "TATASTEEL",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]

NIFTY_NEXT_50 = [
    "ABB", "ABBOTINDIA", "AMBUJACEM", "AUROPHARMA", "BANKBARODA",
    "BERGEPAINT", "BOSCHLTD", "CANBK", "CHOLAFIN", "COLPAL",
    "CONCOR", "DABUR", "DIVISLAB", "DLF", "GAIL",
    "GODREJCP", "HAVELLS", "ICICIPRULI", "INDHOTEL", "IOC",
    "IRCTC", "IRFC", "JIOFIN", "JSWENERGY", "LICI",
    "LODHA", "LUPIN", "MANKIND", "MARICO", "MAXHEALTH",
    "NHPC", "NMDC", "NAUKRI", "OBEROIRLTY", "OFSS",
    "PAGEIND", "PFC", "PIDILITIND", "PNB", "POLYCAB",
    "RECLTD", "SBICARD", "SHREECEM", "SIEMENS", "TORNTPHARM",
    "TVSMOTOR", "UNIONBANK", "VEDL", "VBL", "ZYDUSLIFE",
]

NIFTY_100 = NIFTY_50 + NIFTY_NEXT_50

BSE_ADDITIONAL = [
    "ADANIGREEN", "ADANIPOWER", "ALKEM", "ATUL", "APLAPOLLO",
    "ASTRAL", "AARTI", "BALKRISIND", "BATAINDIA", "BHARATFORG",
    "BIOCON", "CANFINHOME", "CGPOWER", "CUMMINSIND", "DEEPAKNTR",
    "DELHIVERY", "DIXON", "ESCORTS", "EXIDEIND", "FEDERALBNK",
    "FORTIS", "GLENMARK", "GMRINFRA", "GNFC", "GSPL",
    "HAL", "HDFCAMC", "HINDPETRO", "IDFCFIRSTB", "IEX",
    "INDIANB", "INDIAMART", "IPCA", "JUBLFOOD", "KALYANKJIL",
    "KEI", "L&TFH", "LALPATHLAB", "LICHSGFIN", "LINDEINDIA",
    "LTTS", "M&MFIN", "MFSL", "METROPOLIS", "MPHASIS",
    "MRF", "MUTHOOTFIN", "NAM-INDIA", "NATIONALUM", "NAVINFLUOR",
    "PERSISTENT", "PETRONET", "PIIND", "PRESTIGE", "PVRINOX",
    "RAJESHEXPO", "RAMCOCEM", "RVNL", "SAIL", "SOLARINDS",
    "SRF", "STARHEALTH", "SUNDARMFIN", "SUPREMEIND", "SYNGENE",
    "TATACHEM", "TATACOMM", "TATAELXSI", "TATAPOWER", "TORNTPOWER",
    "TRIDENT", "TTML", "UBL", "UNITDSPR", "UPL",
    "VOLTAS", "WHIRLPOOL", "YESBANK", "ZEEL", "ZOMATO",
]

BSE_250 = NIFTY_100 + BSE_ADDITIONAL

UNIVERSES = {
    "nifty50": NIFTY_50,
    "nifty100": NIFTY_100,
    "bse250": BSE_250,
}


def _get_universes() -> dict:
    return UNIVERSES


# ── Data Fetching ──────────────────────────────────────────────────────

def _to_native(val):
    """Convert numpy types to native Python types."""
    if hasattr(val, "item"):
        return val.item()
    return val


def _fetch_stock_data(ticker: str, period: str = "6mo") -> dict | None:
    """Fetch OHLCV data for a single stock. Returns None on failure."""
    try:
        symbol = f"{ticker}.NS"
        t = yf.Ticker(symbol)
        hist = t.history(period=period)
        if hist.empty or len(hist) < 50:
            return None

        closes = hist["Close"].values
        highs = hist["High"].values
        lows = hist["Low"].values
        volumes = hist["Volume"].values

        return {
            "ticker": ticker,
            "symbol": symbol,
            "hist": hist,
            "closes": closes,
            "highs": highs,
            "lows": lows,
            "volumes": volumes,
            "current_close": _to_native(closes[-1]),
            "current_open": _to_native(hist.iloc[-1]["Open"]),
            "current_high": _to_native(highs[-1]),
            "current_low": _to_native(lows[-1]),
            "prev_close": _to_native(closes[-2]),
            "current_volume": _to_native(volumes[-1]),
        }
    except Exception as exc:
        logger.debug("Fetch failed for %s: %s", ticker, exc)
        return None


def _fetch_all_stocks(stocks: list[str], max_workers: int = 10) -> list[dict]:
    """Parallel-fetch OHLCV data for a list of tickers."""
    results = []
    failed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_stock_data, t): t for t in stocks}
        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)
            else:
                failed += 1

    logger.info("Fetched %d/%d stocks (%d failed)", len(results), len(stocks), failed)
    return results


# ── Signal Detection Functions (pure math) ─────────────────────────────

def compute_rsi(closes: np.ndarray, period: int = 14) -> float:
    """Compute RSI from an array of closing prices."""
    if len(closes) < period + 1:
        return 50.0  # neutral default
    deltas = np.diff(closes[-(period + 1):])
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    if down == 0:
        return 100.0
    rs = up / down
    return 100.0 - 100.0 / (1.0 + rs)


def detect_gap(d: dict) -> list[dict]:
    """Detect gap up/down from previous close. Checks if gap filled."""
    signals = []
    current_open = d["current_open"]
    prev_close = d["prev_close"]
    current_close = d["current_close"]

    gap_pct = (current_open - prev_close) / prev_close * 100

    if abs(gap_pct) < 2.0:
        return signals

    if gap_pct > 0:
        # Gap up
        if current_close >= prev_close:
            signals.append({
                "type": "Gap Up (Filled)",
                "direction": "BULLISH",
                "weight_key": "gap_up_filled",
                "value": f"+{gap_pct:.2f}%",
            })
        else:
            signals.append({
                "type": "Gap Up (Unfilled)",
                "direction": "FADE",
                "weight_key": "gap_up_open",
                "value": f"+{gap_pct:.2f}%",
            })
    else:
        # Gap down
        if current_close >= prev_close:
            signals.append({
                "type": "Gap Down (Filled - Reversal)",
                "direction": "BULLISH",
                "weight_key": "gap_down_filled",
                "value": f"{gap_pct:.2f}%",
            })
        else:
            signals.append({
                "type": "Gap Down (Unfilled)",
                "direction": "FADE",
                "weight_key": "gap_down_open",
                "value": f"{gap_pct:.2f}%",
            })

    return signals


def detect_volume_spike(d: dict) -> list[dict]:
    """Detect volume >= 2x 20-day average with price direction."""
    signals = []
    avg_volume = float(np.mean(d["volumes"][-21:-1])) if len(d["volumes"]) > 21 else 0
    if avg_volume <= 0:
        return signals

    vol_ratio = d["current_volume"] / avg_volume
    if vol_ratio < 2.0:
        return signals

    price_change = (d["current_close"] - d["prev_close"]) / d["prev_close"] * 100
    if price_change > 0.5:
        signals.append({
            "type": "Volume Spike (Bullish)",
            "direction": "BULLISH",
            "weight_key": "volume_bullish",
            "value": f"{vol_ratio:.1f}x avg",
        })
    elif price_change < -0.5:
        signals.append({
            "type": "Volume Spike (Bearish)",
            "direction": "BEARISH",
            "weight_key": "volume_bearish",
            "value": f"{vol_ratio:.1f}x avg",
        })

    return signals


def detect_breakout(d: dict) -> list[dict]:
    """Detect 20-day high breakout or breakdown."""
    signals = []
    highs = d["highs"]
    lows = d["lows"]
    if len(highs) < 21 or len(lows) < 21:
        return signals

    n_day_high = float(np.max(highs[-21:-1]))
    n_day_low = float(np.min(lows[-21:-1]))
    avg_volume = float(np.mean(d["volumes"][-21:-1])) if len(d["volumes"]) > 21 else 0

    if d["current_high"] > n_day_high:
        vol_ratio = d["current_volume"] / avg_volume if avg_volume > 0 else 1
        breakout_pct = (d["current_close"] - n_day_high) / n_day_high * 100
        if vol_ratio >= 1.5:
            signals.append({
                "type": "Breakout (Volume Confirmed)",
                "direction": "BULLISH",
                "weight_key": "breakout_vol_confirmed",
                "value": f"+{breakout_pct:.2f}% above 20d high",
            })
        else:
            signals.append({
                "type": "Breakout (Weak Volume)",
                "direction": "BULLISH",
                "weight_key": "breakout_weak",
                "value": f"+{breakout_pct:.2f}% above 20d high",
            })
    elif d["current_low"] < n_day_low:
        breakdown_pct = (d["current_close"] - n_day_low) / n_day_low * 100
        signals.append({
            "type": "Breakdown Below Support",
            "direction": "BEARISH",
            "weight_key": "breakdown_support",
            "value": f"{breakdown_pct:.2f}% below 20d low",
        })

    return signals


def detect_support_resistance(d: dict) -> list[dict]:
    """Detect proximity to 60-day high (resistance) or low (support)."""
    signals = []
    highs = d["highs"]
    lows = d["lows"]
    if len(highs) < 60 or len(lows) < 60:
        return signals

    recent_high = float(np.max(highs[-60:]))
    recent_low = float(np.min(lows[-60:]))
    current_close = d["current_close"]

    distance_to_low = (current_close - recent_low) / current_close * 100
    distance_to_high = (recent_high - current_close) / current_close * 100

    if distance_to_low < 2.0:
        signals.append({
            "type": "Near Major Support",
            "direction": "BULLISH",
            "weight_key": "near_support",
            "value": f"{distance_to_low:.1f}% above low",
        })
    elif distance_to_high < 2.0:
        signals.append({
            "type": "Near Major Resistance",
            "direction": "BEARISH",
            "weight_key": "near_resistance",
            "value": f"{distance_to_high:.1f}% below high",
        })

    return signals


def detect_trend(d: dict) -> list[dict]:
    """Detect strong uptrend or downtrend via SMA 50/200."""
    signals = []
    closes = d["closes"]
    if len(closes) < 200:
        return signals

    sma50 = float(np.mean(closes[-50:]))
    sma200 = float(np.mean(closes[-200:]))
    current_close = d["current_close"]

    if current_close > sma50 > sma200:
        signals.append({
            "type": "Strong Uptrend",
            "direction": "BULLISH",
            "weight_key": "uptrend_strong",
            "value": "Price > 50 SMA > 200 SMA",
        })
    elif current_close < sma50 < sma200:
        signals.append({
            "type": "Strong Downtrend",
            "direction": "BEARISH",
            "weight_key": "downtrend_strong",
            "value": "Price < 50 SMA < 200 SMA",
        })

    return signals


def detect_cyclical(d: dict) -> list[dict]:
    """Detect if current month is historically bullish/bearish for this stock."""
    signals = []
    hist = d["hist"]
    if len(hist) < 50:
        return signals

    current_month = datetime.now().month
    hist_copy = hist.copy()
    hist_copy["Month"] = hist_copy.index.month
    hist_copy["MonthlyReturn"] = hist_copy["Close"].pct_change()
    month_data = hist_copy[hist_copy["Month"] == current_month]["MonthlyReturn"].dropna()

    if len(month_data) > 10:
        avg_month_return = float(month_data.mean() * 100)
        if avg_month_return > 0.2:
            signals.append({
                "type": "Cyclical (Bullish Month)",
                "direction": "BULLISH",
                "weight_key": "cyclical_bullish",
                "value": f"+{avg_month_return:.2f}% historical avg",
            })
        elif avg_month_return < -0.2:
            signals.append({
                "type": "Cyclical (Bearish Month)",
                "direction": "BEARISH",
                "weight_key": "cyclical_bearish",
                "value": f"{avg_month_return:.2f}% historical avg",
            })

    return signals


# ── Scoring Engine ─────────────────────────────────────────────────────

def _classify_direction(score: float) -> str:
    """Classify cumulative score into a direction label."""
    if score >= 4.0:
        return "STRONG BUY"
    elif score >= 2.0:
        return "BUY"
    elif score <= -4.0:
        return "STRONG SELL"
    elif score <= -2.0:
        return "SELL"
    return "NEUTRAL"


def score_stock(d: dict, weights: dict | None = None) -> dict | None:
    """Score a single stock using all signal detectors.

    Args:
        d: Dict from _fetch_stock_data with OHLCV arrays.
        weights: Signal weights override. Defaults to DEFAULT_WEIGHTS.

    Returns:
        Dict with ticker, score, direction, confidence, signals, etc.
        Returns None if data is insufficient.
    """
    w = weights or DEFAULT_WEIGHTS
    all_signals = []
    score = 0.0

    # Run all detectors
    for detector in (
        detect_gap,
        detect_volume_spike,
        detect_breakout,
        detect_support_resistance,
        detect_trend,
        detect_cyclical,
    ):
        for sig in detector(d):
            weight = w.get(sig["weight_key"], 0)
            score += weight
            all_signals.append({**sig, "weight": weight})

    # RSI (special case — needs closes array)
    rsi = compute_rsi(d["closes"])
    if rsi < 30:
        weight = w["rsi_oversold"]
        score += weight
        all_signals.append({
            "type": "RSI Oversold",
            "direction": "BULLISH",
            "weight_key": "rsi_oversold",
            "value": f"RSI {rsi:.1f}",
            "weight": weight,
        })
    elif rsi > 70:
        weight = w["rsi_overbought"]
        score += weight
        all_signals.append({
            "type": "RSI Overbought",
            "direction": "BEARISH",
            "weight_key": "rsi_overbought",
            "value": f"RSI {rsi:.1f}",
            "weight": weight,
        })

    # Direction and confidence
    direction = _classify_direction(score)
    bullish = [s for s in all_signals if s["direction"] == "BULLISH"]
    bearish = [s for s in all_signals if s["direction"] == "BEARISH"]
    aligned = max(len(bullish), len(bearish))
    confidence = "HIGH" if aligned >= 4 else ("MEDIUM" if aligned >= 2 else "LOW")

    # Success probability
    abs_score = abs(score)
    base_prob = 50.0
    score_edge = min(abs_score * 4, 30)
    alignment_bonus = min(aligned * 2, 15)
    success_probability = round(min(base_prob + score_edge + alignment_bonus, 85), 0)
    if abs_score < 2:
        success_probability = 50

    price_change_day = (d["current_close"] - d["prev_close"]) / d["prev_close"] * 100

    return {
        "ticker": d["ticker"],
        "symbol": d["symbol"],
        "price": round(d["current_close"], 2),
        "change_pct": round(price_change_day, 2),
        "rsi": round(rsi, 1),
        "score": round(score, 2),
        "direction": direction,
        "confidence": confidence,
        "success_probability": int(success_probability),
        "signals": all_signals,
        "bullish_signal_count": len(bullish),
        "bearish_signal_count": len(bearish),
    }


# ── Market Regime Classifier ───────────────────────────────────────────

NIFTY_SYMBOL = "^NSEI"
HIGH_VOL_MULTIPLIER = 1.5


def _annualized_vol(closes: np.ndarray, window: int = 20) -> float:
    """Annualized realized volatility over trailing window days."""
    if len(closes) < window + 1:
        return 0.0
    rets = np.diff(closes[-window - 1:]) / closes[-window - 1:-1]
    return float(np.std(rets) * np.sqrt(252) * 100)


def get_market_regime() -> dict:
    """Classify current Nifty regime as BULL/BEAR/SIDEWAYS/HIGH_VOL.

    Uses Nifty index data (^NSEI) from yfinance.
    """
    try:
        hist = yf.Ticker(NIFTY_SYMBOL).history(period="1y")
    except Exception as e:
        return {"regime": "UNKNOWN", "reasoning": f"fetch failed: {e}"}

    if hist.empty or len(hist) < 200:
        return {"regime": "UNKNOWN", "reasoning": f"insufficient data ({len(hist)} bars)"}

    closes = hist["Close"].values
    nifty_close = float(closes[-1])
    sma_50 = float(np.mean(closes[-50:]))
    sma_200 = float(np.mean(closes[-200:]))

    # Volatility
    current_vol = _annualized_vol(closes, window=20)
    baseline_vols = []
    for i in range(120, 20, -5):
        if i + 20 < len(closes):
            baseline_vols.append(_annualized_vol(closes[:-i] if i else closes, window=20))
    vol_baseline = float(np.mean([v for v in baseline_vols if v > 0])) if baseline_vols else current_vol

    # Classify
    if vol_baseline > 0 and current_vol > vol_baseline * HIGH_VOL_MULTIPLIER:
        regime = "HIGH_VOL"
        reasoning = f"Vol {current_vol:.1f}% > {HIGH_VOL_MULTIPLIER}x baseline ({vol_baseline:.1f}%)"
    elif nifty_close > sma_50 > sma_200:
        regime = "BULL"
        reasoning = f"Nifty {nifty_close:.0f} > 50 SMA {sma_50:.0f} > 200 SMA {sma_200:.0f}"
    elif nifty_close < sma_50 < sma_200:
        regime = "BEAR"
        reasoning = f"Nifty {nifty_close:.0f} < 50 SMA {sma_50:.0f} < 200 SMA {sma_200:.0f}"
    else:
        regime = "SIDEWAYS"
        reasoning = f"Mixed trend: Nifty {nifty_close:.0f}, 50 SMA {sma_50:.0f}, 200 SMA {sma_200:.0f}"

    return {
        "regime": regime,
        "nifty_close": round(nifty_close, 2),
        "sma_50": round(sma_50, 2),
        "sma_200": round(sma_200, 2),
        "annualized_vol_pct": round(current_vol, 2),
        "reasoning": reasoning,
    }


# ── Event Filter ───────────────────────────────────────────────────────
# Hardcoded market-wide event dates (RBI, Budget, FOMC, F&O expiry).
# Earnings are fetched live per ticker via yfinance.

RBI_POLICY_DATES = [
    "2025-02-07", "2025-04-09", "2025-06-06", "2025-08-08", "2025-10-09", "2025-12-05",
    "2026-02-06", "2026-04-09", "2026-06-05", "2026-08-07", "2026-10-08", "2026-12-04",
]

UNION_BUDGET_DATES = ["2025-02-01", "2026-02-01", "2027-02-01"]

FED_FOMC_DATES = [
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30",
    "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-05-06", "2026-06-17", "2026-07-29",
    "2026-09-16", "2026-10-28", "2026-12-09",
]


def _get_monthly_expiry_dates(year: int) -> list[str]:
    """Generate last Thursday of each month for F&O expiry."""
    dates = []
    for month in range(1, 13):
        if month == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)
        d = next_month - timedelta(days=1)
        while d.weekday() != 3:
            d -= timedelta(days=1)
        dates.append(d.strftime("%Y-%m-%d"))
    return dates


def _fetch_earnings_for_ticker(ticker: str) -> Optional[str]:
    """Fetch next earnings date via yfinance. Returns date string or None."""
    try:
        t = yf.Ticker(f"{ticker}.NS")
        cal = t.calendar
        if not cal or not isinstance(cal, dict):
            return None
        earnings_date = cal.get("Earnings Date")
        if not earnings_date:
            return None
        if isinstance(earnings_date, list):
            earnings_date = earnings_date[0] if earnings_date else None
        if not earnings_date:
            return None
        if hasattr(earnings_date, "strftime"):
            return earnings_date.strftime("%Y-%m-%d")
        return str(earnings_date)[:10]
    except Exception:
        return None


def get_event_penalty(ticker: str, days_ahead: int = 2) -> dict:
    """Check if a ticker has events in the next N days.

    Returns:
        {
            "has_event": bool,
            "score_adjustment": float (negative for risky events),
            "events": list,
            "warning": str or None,
        }
    """
    today = date.today()
    end = today + timedelta(days=days_ahead)
    warnings = []
    score_adj = 0.0
    events_found = []

    # Check stock-specific earnings
    earnings_date = _fetch_earnings_for_ticker(ticker)
    if earnings_date:
        try:
            e_date = datetime.strptime(earnings_date, "%Y-%m-%d").date()
            days_until = (e_date - today).days
            if 0 <= days_until <= days_ahead:
                warnings.append(f"Earnings in {days_until} day{'s' if days_until != 1 else ''}")
                score_adj -= 2.5
                events_found.append({"type": "EARNINGS", "date": earnings_date, "days_until": days_until})
        except (ValueError, TypeError):
            pass

    # Check market-wide events
    def in_range(date_str: str) -> bool:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            return today <= d <= end
        except Exception:
            return False

    for d in RBI_POLICY_DATES:
        if in_range(d):
            days_until = (datetime.strptime(d, "%Y-%m-%d").date() - today).days
            if days_until == 0:
                warnings.append("RBI policy decision today")
                score_adj -= 1.5
                events_found.append({"type": "RBI_POLICY", "date": d, "days_until": days_until})

    for d in UNION_BUDGET_DATES:
        if in_range(d):
            days_until = (datetime.strptime(d, "%Y-%m-%d").date() - today).days
            if days_until <= 1:
                warnings.append(f"Budget in {days_until} day(s)")
                score_adj -= 2.0
                events_found.append({"type": "BUDGET", "date": d, "days_until": days_until})

    for d in FED_FOMC_DATES:
        if in_range(d):
            days_until = (datetime.strptime(d, "%Y-%m-%d").date() - today).days
            if days_until == 0:
                warnings.append("Fed FOMC decision today")
                score_adj -= 1.0
                events_found.append({"type": "FOMC", "date": d, "days_until": days_until})

    for year in range(today.year, end.year + 1):
        for d in _get_monthly_expiry_dates(year):
            if in_range(d):
                days_until = (datetime.strptime(d, "%Y-%m-%d").date() - today).days
                if days_until == 0:
                    warnings.append("F&O expiry today")
                    score_adj -= 0.5
                    events_found.append({"type": "FNO_EXPIRY", "date": d, "days_until": days_until})

    return {
        "has_event": len(events_found) > 0,
        "score_adjustment": round(score_adj, 2),
        "events": events_found,
        "warning": " | ".join(warnings) if warnings else None,
    }


# ── FII/DII Bias ───────────────────────────────────────────────────────

def get_fii_dii_bias() -> dict:
    """Compute FII/DII market bias from NSE data.

    Wraps the existing rbi.py data vendor which fetches from NSE endpoints.
    """
    try:
        from tradingagents.dataflows.rbi import _fetch_nse_fii_dii
        raw = _fetch_nse_fii_dii()
    except Exception as e:
        logger.debug("FII/DII fetch failed: %s", e)
        return {
            "bias": "NEUTRAL",
            "confidence": "NONE",
            "score_adjustment": 0,
            "reasoning": "FII/DII data unavailable",
        }

    # Parse the raw NSE JSON — format varies but typically has category/buyValue/sellValue/netValue
    try:
        records = raw if isinstance(raw, list) else []
        if not records:
            return {
                "bias": "NEUTRAL",
                "confidence": "NONE",
                "score_adjustment": 0,
                "reasoning": "No FII/DII records found",
            }

        # Find FII and DII entries
        fii_net = 0.0
        dii_net = 0.0
        for rec in records:
            cat = str(rec.get("category", rec.get("Category", ""))).upper()
            net_val = rec.get("netValue", rec.get("Net Value", rec.get("net", 0)))
            try:
                net_val = float(str(net_val).replace(",", "") or 0)
            except (ValueError, TypeError):
                net_val = 0.0
            if "FII" in cat or "FPI" in cat:
                fii_net = net_val
            elif "DII" in cat:
                dii_net = net_val

        # Compute bias
        bias = "NEUTRAL"
        confidence = "LOW"
        score_adj = 0.0
        reasoning_parts = []

        if fii_net < -2000:
            bias, confidence, score_adj = "BEARISH", "HIGH", -1.5
            reasoning_parts.append(f"FIIs selling heavily ({fii_net:,.0f} Cr)")
        elif fii_net < -1000:
            bias, confidence, score_adj = "BEARISH", "MEDIUM", -1.0
            reasoning_parts.append(f"FIIs net sellers ({fii_net:,.0f} Cr)")
        elif fii_net > 2000:
            bias, confidence, score_adj = "BULLISH", "HIGH", 1.5
            reasoning_parts.append(f"FIIs buying aggressively (+{fii_net:,.0f} Cr)")
        elif fii_net > 1000:
            bias, confidence, score_adj = "BULLISH", "MEDIUM", 1.0
            reasoning_parts.append(f"FIIs net buyers (+{fii_net:,.0f} Cr)")

        # DII offset
        if bias == "BEARISH" and dii_net > abs(fii_net) * 0.7:
            score_adj *= 0.5
            reasoning_parts.append(f"DIIs absorbing selling (+{dii_net:,.0f} Cr)")
        elif bias == "BULLISH" and dii_net < -abs(fii_net) * 0.5:
            score_adj *= 0.5
            reasoning_parts.append(f"But DIIs selling ({dii_net:,.0f} Cr)")

        if not reasoning_parts:
            reasoning_parts.append("FII/DII flows neutral")

        return {
            "bias": bias,
            "confidence": confidence,
            "score_adjustment": round(score_adj, 2),
            "reasoning": ". ".join(reasoning_parts),
            "fii_net": fii_net,
            "dii_net": dii_net,
        }
    except Exception as e:
        logger.debug("FII/DII parse failed: %s", e)
        return {
            "bias": "NEUTRAL",
            "confidence": "NONE",
            "score_adjustment": 0,
            "reasoning": "FII/DII data parse error",
        }


# ── Score Adjustment Helpers ───────────────────────────────────────────

def _apply_bias(result: dict, bias: dict) -> dict:
    """Apply FII/DII market bias to a stock's score."""
    adj = bias.get("score_adjustment", 0)
    if adj == 0:
        return result

    new_score = round(result["score"] + adj, 2)
    result["signals"].append({
        "type": f"FII/DII Flow ({bias['bias']})",
        "direction": "BULLISH" if adj > 0 else "BEARISH",
        "value": bias["reasoning"],
        "weight": adj,
    })
    result["score"] = new_score
    result["direction"] = _classify_direction(new_score)
    if adj > 0:
        result["bullish_signal_count"] += 1
    elif adj < 0:
        result["bearish_signal_count"] += 1
    return result


def _apply_event_penalty(result: dict, event_filter: dict) -> dict:
    """Apply event-based score penalty to a stock."""
    adj = event_filter.get("score_adjustment", 0)
    if adj == 0:
        return result

    new_score = round(result["score"] + adj, 2)
    warning = event_filter.get("warning") or "Upcoming event"
    result["signals"].append({
        "type": f"Event Risk ({warning})",
        "direction": "BEARISH",
        "value": warning,
        "weight": adj,
    })
    result["score"] = new_score
    result["direction"] = _classify_direction(new_score)
    result["bearish_signal_count"] += 1
    return result


# ── Entry Points ───────────────────────────────────────────────────────

def run_full_scan(
    universe: str = "nifty100",
    max_workers: int = 10,
) -> dict:
    """Scan all stocks in a universe, score each one, return ranked results.

    Args:
        universe: "nifty50", "nifty100", or "bse250"
        max_workers: Parallel fetch threads.

    Returns:
        {
            "universe": str,
            "total_stocks": int,
            "scored": int,
            "regime": dict,
            "fii_dii_bias": dict,
            "strong_buys": [...],
            "buys": [...],
            "sells": [...],
            "strong_sells": [...],
            "neutrals": [...],
        }
    """
    universes = _get_universes()
    stocks = universes.get(universe, universes.get("nifty100", []))

    logger.info("Starting full scan: %d stocks from %s", len(stocks), universe)

    # Fetch regime and FII/DII once (used for all stocks)
    regime = get_market_regime()
    fii_dii_bias = get_fii_dii_bias()
    logger.info("Market regime: %s | FII/DII: %s", regime.get("regime"), fii_dii_bias.get("bias"))

    # Parallel fetch all stock data
    all_data = _fetch_all_stocks(stocks, max_workers=max_workers)

    # Score each stock
    all_results = []
    for d in all_data:
        result = score_stock(d)
        if result is None:
            continue

        # Apply filters
        result = _apply_bias(result, fii_dii_bias)

        event_filter = get_event_penalty(result["ticker"])
        if event_filter["has_event"]:
            result = _apply_event_penalty(result, event_filter)

        all_results.append(result)

    # Sort into buckets
    strong_buys = sorted([r for r in all_results if r["direction"] == "STRONG BUY"], key=lambda x: -x["score"])
    buys = sorted([r for r in all_results if r["direction"] == "BUY"], key=lambda x: -x["score"])
    sells = sorted([r for r in all_results if r["direction"] == "SELL"], key=lambda x: x["score"])
    strong_sells = sorted([r for r in all_results if r["direction"] == "STRONG SELL"], key=lambda x: x["score"])
    neutrals = sorted([r for r in all_results if r["direction"] == "NEUTRAL"], key=lambda x: -x["score"])

    logger.info(
        "Scan complete: %d scored | STRONG BUY: %d | BUY: %d | SELL: %d | STRONG SELL: %d",
        len(all_results), len(strong_buys), len(buys), len(sells), len(strong_sells),
    )

    return {
        "universe": universe,
        "total_stocks": len(stocks),
        "scored": len(all_results),
        "regime": regime,
        "fii_dii_bias": fii_dii_bias,
        "strong_buys": strong_buys,
        "buys": buys,
        "sells": sells,
        "strong_sells": strong_sells,
        "neutrals": neutrals,
    }


def get_buy_list(
    universe: str = "nifty100",
    min_score: float = 2.0,
    max_stocks: int = 25,
) -> list[dict]:
    """Get ranked BUY list for the batch runner.

    Runs full scan, filters to BUY+ only, returns tickers with scores.
    This is the primary entry point for batch_runner.py.

    Args:
        universe: Stock universe to scan.
        min_score: Minimum score to include (default 2.0 = BUY threshold).
        max_stocks: Maximum stocks to return.

    Returns:
        List of dicts sorted by score descending:
        [{"ticker": "RELIANCE", "score": 4.5, "direction": "STRONG BUY", ...}]
    """
    scan = run_full_scan(universe=universe)

    buy_list = scan["strong_buys"] + scan["buys"]
    buy_list = [r for r in buy_list if r["score"] >= min_score]
    buy_list.sort(key=lambda x: -x["score"])

    if len(buy_list) > max_stocks:
        buy_list = buy_list[:max_stocks]

    logger.info("Buy list: %d stocks (min_score=%.1f)", len(buy_list), min_score)
    return buy_list


def get_quick_scans(universe: str = "nifty50") -> dict:
    """Standalone gap/volume/breakout scanners without full scoring.

    Returns quick hit lists for each strategy.
    """
    universes = _get_universes()
    stocks = universes.get(universe, universes.get("nifty50", []))

    all_data = _fetch_all_stocks(stocks, max_workers=10)

    gaps = []
    volume_spikes = []
    breakouts = []

    for d in all_data:
        # Gaps
        for sig in detect_gap(d):
            gaps.append({
                "ticker": d["ticker"],
                "price": round(d["current_close"], 2),
                "signal": sig["type"],
                "value": sig["value"],
            })

        # Volume spikes
        for sig in detect_volume_spike(d):
            volume_spikes.append({
                "ticker": d["ticker"],
                "price": round(d["current_close"], 2),
                "signal": sig["type"],
                "value": sig["value"],
            })

        # Breakouts
        for sig in detect_breakout(d):
            breakouts.append({
                "ticker": d["ticker"],
                "price": round(d["current_close"], 2),
                "signal": sig["type"],
                "value": sig["value"],
            })

    return {
        "universe": universe,
        "scanned": len(all_data),
        "gap": gaps,
        "volume": volume_spikes,
        "breakout": breakouts,
    }
