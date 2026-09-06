"""Batch runner for daily stock scanning.

Runs the TradingAgents analysis on a list of stocks, sends Telegram
alerts for buy/overweight signals, and logs errors.
"""

from __future__ import annotations

import json
import logging
import re
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Maps graph node names to (friendly stage name, report group) for /report
NODE_STAGE_MAP = {
    "Market Analyst":          ("Market Analyst",       "analysts"),
    "Msg Clear Market":       (None,                   None),
    "tools_market":           (None,                   None),
    "Sentiment Analyst":      ("Sentiment Analyst",    "analysts"),
    "Msg Clear Sentiment":    (None,                   None),
    "tools_social":           (None,                   None),
    "News Analyst":           ("News Analyst",         "analysts"),
    "Msg Clear News":         (None,                   None),
    "tools_news":             (None,                   None),
    "Fundamentals Analyst":   ("Fundamentals Analyst", "analysts"),
    "Msg Clear Fundamentals": (None,                   None),
    "tools_fundamentals":     (None,                   None),
    "Bull Researcher":        ("Bull/Bear Debate",     "research"),
    "Bear Researcher":        ("Bull/Bear Debate",     "research"),
    "Research Manager":       ("Research Decision",    "research"),
    "Trader":                 ("Trader",               "trading"),
    "Aggressive Analyst":     ("Risk Debate",          "risk"),
    "Conservative Analyst":   ("Risk Debate",          "risk"),
    "Neutral Analyst":        ("Risk Debate",          "risk"),
    "Portfolio Manager":      ("Portfolio Manager",    "portfolio"),
}

# Maps report groups to the state keys they produce
GROUP_REPORT_KEYS = {
    "analysts": ["market_report", "sentiment_report", "news_report", "fundamentals_report"],
    "research": ["investment_debate_state"],
    "trading":  ["trader_investment_plan"],
    "risk":     ["risk_debate_state"],
    "portfolio": ["final_trade_decision"],
}

STAGE_ORDER = ["analysts", "research", "trading", "risk", "portfolio"]


def _ensure_indian_suffix(ticker: str) -> str:
    """Append .NS suffix to bare Indian stock tickers for Yahoo Finance.

    The stock scanner returns bare tickers (e.g. "RELIANCE") but Yahoo
    Finance requires the exchange suffix (e.g. "RELIANCE.NS") for Indian
    NSE stocks.  Tickers that already carry a suffix are returned as-is.
    """
    upper = ticker.upper()
    if upper.endswith((".NS", ".BO", ".NSE")):
        return ticker
    return f"{ticker}.NS"


def _extract_from_markdown(text: str, pattern: str) -> Optional[str]:
    """Extract a value from markdown like **Key**: Value."""
    match = re.search(rf"\*\*{pattern}\*\*:\s*(.+)", text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _extract_float(text: str, pattern: str) -> Optional[float]:
    val = _extract_from_markdown(text, pattern)
    if val is None:
        return None
    try:
        return float(val.replace(",", "").replace("₹", "").replace("$", ""))
    except (ValueError, TypeError):
        return None


def _parse_trader_plan(plan_text: str) -> dict:
    return {
        "entry_price": _extract_float(plan_text, "Entry Price"),
        "stop_loss": _extract_float(plan_text, "Stop Loss"),
        "position_sizing": _extract_from_markdown(plan_text, "Position Sizing"),
    }


def _parse_portfolio_decision(decision_text: str) -> dict:
    rating = _extract_from_markdown(decision_text, "Rating")
    return {
        "rating": rating,
        "price_target": _extract_float(decision_text, "Price Target"),
        "time_horizon": _extract_from_markdown(decision_text, "Time Horizon"),
        "summary": _extract_from_markdown(decision_text, "Executive Summary"),
        "thesis": _extract_from_markdown(decision_text, "Investment Thesis"),
    }


def _truncate(text: str, max_chars: int = 500) -> str:
    """Truncate text for log display."""
    if not text:
        return "(empty)"
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... [{len(text)} total chars]"


def _load_progress(progress_path: Path) -> dict:
    if progress_path.exists():
        with open(progress_path, encoding="utf-8") as f:
            return json.load(f)
    return {"analyzed": [], "signals": [], "errors": [], "started_at": None}


def _save_progress(progress_path: Path, progress: dict):
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    with open(progress_path, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2, default=str)


def _setup_logging(results_dir: str) -> Path:
    """Configure logging to both console and daily log file."""
    log_dir = Path(results_dir) / "daily" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = log_dir / f"scan_{today}.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # File handler — detailed logs
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    root_logger.addHandler(fh)

    # Console handler — info and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    root_logger.addHandler(ch)

    return log_file


def _save_stock_report(ticker: str, final_state: dict, signal: str, config: dict):
    """Save full report tree for a single stock."""
    from tradingagents.reporting import write_report_tree

    today = datetime.now().strftime("%Y-%m-%d")
    save_dir = Path(config["results_dir"]) / "daily" / today / ticker
    try:
        report_path = write_report_tree(final_state, ticker, save_dir)
        logger.debug("Report saved: %s", report_path)
        return report_path
    except Exception as exc:
        logger.warning("Could not save report for %s: %s", ticker, exc)
        return None


def _save_partial_state(ticker: str, partial_state: dict, config: dict):
    """Write partial state to disk so /report can read it mid-analysis."""
    today = datetime.now().strftime("%Y-%m-%d")
    partial_dir = Path(config["results_dir"]) / "daily" / today
    partial_dir.mkdir(parents=True, exist_ok=True)
    partial_path = partial_dir / f"{ticker}_partial.json"

    # Only write non-empty fields
    serializable = {}
    for key in ("market_report", "sentiment_report", "news_report",
                "fundamentals_report", "investment_debate_state",
                "trader_investment_plan", "risk_debate_state",
                "final_trade_decision"):
        val = partial_state.get(key)
        if val:
            serializable[key] = val

    with open(partial_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2, default=str)
    return partial_path


def _get_completed_groups(partial_state: dict) -> list[str]:
    """Return list of completed report groups based on state keys."""
    completed = []
    for group, keys in GROUP_REPORT_KEYS.items():
        if group == "analysts":
            # All 4 analyst reports must be present to mark analysts as complete
            if all(partial_state.get(k) for k in keys):
                completed.append(group)
        elif group == "research":
            debate = partial_state.get("investment_debate_state", {})
            if debate and (debate.get("judge_decision") or debate.get("bull_history")):
                completed.append(group)
        elif group == "risk":
            risk = partial_state.get("risk_debate_state", {})
            if risk and (risk.get("judge_decision") or risk.get("aggressive_history")):
                completed.append(group)
        elif group == "portfolio":
            if partial_state.get("final_trade_decision"):
                completed.append(group)
        else:
            if partial_state.get(keys[0]):
                completed.append(group)
    return completed


def _current_stage_name(completed_groups: list[str]) -> str:
    """Return the friendly name of the current stage being worked on."""
    for group in STAGE_ORDER:
        if group not in completed_groups:
            stage_entries = [(k, v[0]) for k, v in NODE_STAGE_MAP.items() if v[1] == group and v[0]]
            if stage_entries:
                return stage_entries[0][1]
    return "Complete"


def _log_analyst_reports(final_state: dict, ticker: str):
    """Log each analyst's report summary."""
    reports = {
        "Market Analyst": final_state.get("market_report", ""),
        "Sentiment Analyst": final_state.get("sentiment_report", ""),
        "News Analyst": final_state.get("news_report", ""),
        "Fundamentals Analyst": final_state.get("fundamentals_report", ""),
    }

    logger.info("  --- Analyst Reports ---")
    for name, report in reports.items():
        if report:
            logger.info("  [%s] %s", name, _truncate(report, 300))
        else:
            logger.warning("  [%s] No report generated", name)


def _log_debate_outcomes(final_state: dict, ticker: str):
    """Log bull/bear and risk debate outcomes."""
    debate = final_state.get("investment_debate_state", {})
    if debate:
        judge = debate.get("judge_decision", "")
        if judge:
            logger.info("  [Research Manager] %s", _truncate(judge, 400))
        else:
            logger.warning("  [Research Debate] No judge decision")

    risk = final_state.get("risk_debate_state", {})
    if risk:
        judge = risk.get("judge_decision", "")
        if judge:
            logger.info("  [Portfolio Manager] %s", _truncate(judge, 400))
        else:
            logger.warning("  [Risk Debate] No judge decision")


def _log_final_decision(final_state: dict, signal: str, ticker: str):
    """Log the final trade decision."""
    decision = final_state.get("final_trade_decision", "")
    logger.info("  --- Final Decision ---")
    logger.info("  Signal: %s", signal)
    if decision:
        logger.info("  Full decision: %s", _truncate(decision, 600))


def run_daily_scan(
    max_stocks: Optional[int] = None,
    config: Optional[dict] = None,
    dry_run: bool = False,
):
    """Run the daily scan: screener -> analyze -> notify."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.screener.sector_momentum import get_todays_stocks, rank_sectors
    from tradingagents.notifications.telegram import (
        send_alert,
        send_error,
        send_daily_summary,
    )

    if config is None:
        config = DEFAULT_CONFIG.copy()

    log_file = _setup_logging(config["results_dir"])
    logger.info("Log file: %s", log_file)

    max_daily = max_stocks or config.get("max_daily_stocks", 25)
    today = datetime.now().strftime("%Y-%m-%d")

    # Progress file for resume support
    daily_dir = Path(config["results_dir"]) / "daily"
    progress_path = daily_dir / f"{today}_progress.json"
    progress = _load_progress(progress_path)

    if not progress["started_at"]:
        progress["started_at"] = datetime.now().isoformat()
        _save_progress(progress_path, progress)

    already_done = set(progress["analyzed"])
    logger.info(
        "Starting daily scan for %s — %d already done",
        today, len(already_done),
    )

    # --- Step 1: Get stocks from scanner (multi-signal first, sector momentum fallback) ---
    stocks = []
    try:
        from tradingagents.screener.stock_scanner import get_buy_list
        buy_list = get_buy_list(
            universe=config.get("scan_universe", "nifty100"),
            min_score=2.0,
            max_stocks=max_daily,
        )
        stocks = [_ensure_indian_suffix(item["ticker"]) for item in buy_list]
        logger.info("Multi-signal scanner returned %d BUY+ stocks", len(stocks))
    except Exception as exc:
        logger.warning("Multi-signal scanner failed, falling back to sector momentum: %s", exc)

    if not stocks:
        try:
            stocks = get_todays_stocks(
                exclude_tickers=list(already_done),
            )
            logger.info("Sector momentum fallback returned %d stocks", len(stocks))
        except Exception as exc:
            msg = f"Both scanners failed: {exc}"
            logger.error(msg)
            send_error(msg, level="critical")
            return

    if not stocks:
        msg = "Both scanners returned 0 stocks."
        logger.warning(msg)
        if not dry_run:
            send_error(msg, level="warning")
        return

    # Limit to max_daily
    remaining = max_daily - len(already_done)
    if remaining <= 0:
        logger.info("Already hit daily limit (%d stocks)", max_daily)
        return
    stocks = stocks[:remaining]

    logger.info("Will analyze %d stocks: %s", len(stocks), stocks)

    # Write total_stocks to progress for status display
    progress["total_stocks"] = len(stocks)
    _save_progress(progress_path, progress)

    # --- Dry run: show what would be analyzed and stop ---
    if dry_run:
        logger.info("=== DRY RUN — not analyzing ===")
        rankings = rank_sectors()
        logger.info("\nSector Rankings:")
        for i, r in enumerate(rankings, 1):
            logger.info(
                "  %d. %s — score: %s (5d: %s%%, 20d: %s%%)",
                i, r["name"], r["score"], r["ret_5d"], r["ret_20d"],
            )
        logger.info("\nStocks to analyze:")
        for i, s in enumerate(stocks, 1):
            logger.info("  %d. %s", i, s)
        logger.info("=== End Dry Run ===")
        return

    # --- Step 2: Analyze each stock ---
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    ta = TradingAgentsGraph(debug=False, config=config)
    buy_signals = []
    all_results = []

    for i, ticker in enumerate(stocks, 1):
        logger.info("=" * 60)
        logger.info("[%d/%d] ANALYZING: %s", i, len(stocks), ticker)
        logger.info("=" * 60)
        start = time.time()

        # Update current_stock for live status
        progress["current_stock"] = ticker
        progress["current_index"] = i
        # Update per-stock entry
        if "stocks" not in progress:
            progress["stocks"] = {}
        progress["stocks"][ticker] = {
            "status": "in_progress",
            "stage": "Starting...",
            "completed_groups": [],
            "elapsed_seconds": 0,
        }
        _save_progress(progress_path, progress)

        try:
            # Use streaming to capture partial state for /report
            partial_state = {}
            final_state = None
            for chunk, node_name in ta.stream_propagate(ticker, today):
                # Merge chunk into partial state
                for key, val in chunk.items():
                    if val is not None:
                        partial_state[key] = val

                # Track stage progress
                if node_name and node_name in NODE_STAGE_MAP:
                    stage_name, group = NODE_STAGE_MAP[node_name]
                    if stage_name:
                        logger.info("  [Stage] %s — %s", ticker, stage_name)
                    # Write partial state after each meaningful node
                    if group:
                        _save_partial_state(ticker, partial_state, config)
                        completed = _get_completed_groups(partial_state)
                        current = _current_stage_name(completed)
                        progress["stocks"][ticker]["stage"] = current
                        progress["stocks"][ticker]["completed_groups"] = completed
                        progress["stocks"][ticker]["elapsed_seconds"] = round(time.time() - start, 1)
                        _save_progress(progress_path, progress)

            # Streaming done — partial_state is now the final state
            final_state = partial_state
            signal = ta.process_signal(final_state.get("final_trade_decision", ""))
            elapsed = time.time() - start

            # Store decision for memory log (like _run_graph does)
            ta.memory_log.store_decision(
                ticker=ticker,
                trade_date=today,
                final_trade_decision=final_state.get("final_trade_decision", ""),
            )
            # Clear checkpoint on success
            ta.clear_checkpoint_on_success(ticker, today)

            _log_analyst_reports(final_state, ticker)
            _log_debate_outcomes(final_state, ticker)
            _log_final_decision(final_state, signal, ticker)

            trader_data = _parse_trader_plan(
                final_state.get("trader_investment_plan", "")
            )
            pm_data = _parse_portfolio_decision(
                final_state.get("final_trade_decision", "")
            )

            result = {
                "ticker": ticker,
                "signal": signal,
                "rating": pm_data.get("rating"),
                "price_target": pm_data.get("price_target"),
                "time_horizon": pm_data.get("time_horizon"),
                "entry_price": trader_data.get("entry_price"),
                "stop_loss": trader_data.get("stop_loss"),
                "position_sizing": trader_data.get("position_sizing"),
                "summary": pm_data.get("summary", ""),
                "thesis": pm_data.get("thesis", ""),
                "elapsed_seconds": round(elapsed, 1),
            }
            all_results.append(result)

            report_path = _save_stock_report(ticker, final_state, signal, config)

            progress["analyzed"].append(ticker)
            # Mark stock as completed in progress
            if "stocks" in progress and ticker in progress["stocks"]:
                progress["stocks"][ticker]["status"] = "completed"
                progress["stocks"][ticker]["signal"] = signal
                progress["stocks"][ticker]["stage"] = "Complete"
                progress["stocks"][ticker]["completed_groups"] = STAGE_ORDER[:]
                progress["stocks"][ticker]["elapsed_seconds"] = round(elapsed, 1)
                if report_path:
                    progress["stocks"][ticker]["report_path"] = str(report_path)
            logger.info("--- %s RESULT: %s (%.1fs) ---", ticker, signal, elapsed)
            if report_path:
                logger.info("  Report saved: %s", report_path)

            if signal in ("Buy", "Overweight"):
                progress["signals"].append(result)
                buy_signals.append(result)
                send_alert(
                    ticker=ticker,
                    rating=signal,
                    price_target=pm_data.get("price_target"),
                    entry_price=trader_data.get("entry_price"),
                    stop_loss=trader_data.get("stop_loss"),
                    time_horizon=pm_data.get("time_horizon"),
                    summary=pm_data.get("summary", ""),
                )

        except Exception as exc:
            elapsed = time.time() - start
            error_msg = f"{ticker} analysis failed after {elapsed:.1f}s: {exc}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            progress["errors"].append(error_msg)
            progress["analyzed"].append(ticker)
            # Mark stock as error in progress
            if "stocks" in progress and ticker in progress["stocks"]:
                progress["stocks"][ticker]["status"] = "error"
                progress["stocks"][ticker]["stage"] = "Failed"
                progress["stocks"][ticker]["elapsed_seconds"] = round(elapsed, 1)
                progress["stocks"][ticker]["error"] = error_msg
            send_error(error_msg, level="warning")

        _save_progress(progress_path, progress)

    # --- Step 3: End-of-day summary ---
    progress["completed_at"] = datetime.now().isoformat()
    progress["current_stock"] = None
    progress["current_index"] = None
    _save_progress(progress_path, progress)

    total_analyzed = len(progress["analyzed"])
    all_signals = progress.get("signals", [])
    all_errors = progress.get("errors", [])

    logger.info("=" * 60)
    logger.info("DAILY SCAN COMPLETE")
    logger.info("  Analyzed: %d stocks", total_analyzed)
    logger.info("  Buy signals: %d", len(all_signals))
    logger.info("  Errors: %d", len(all_errors))
    if all_signals:
        logger.info("  Buy signal tickers: %s", [s["ticker"] for s in all_signals])
    logger.info("=" * 60)

    send_daily_summary(
        stocks_analyzed=total_analyzed,
        buy_signals=all_signals,
        errors=all_errors,
    )


def resume_scan(config: Optional[dict] = None, dry_run: bool = False):
    """Resume an interrupted scan (skips already-analyzed stocks)."""
    run_daily_scan(config=config, dry_run=dry_run)


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    run_daily_scan(dry_run=dry)
