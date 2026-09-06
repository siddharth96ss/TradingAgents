"""Tests for the /report feature: streaming, partial state, and report generation.

Covers:
- stream_propagate yields per-node chunks
- NODE_STAGE_MAP covers all graph nodes
- Stage tracking helpers (_get_completed_groups, _current_stage_name)
- Partial state writing to disk
- Partial report file generation from partial state
- Telegram bot /report command and callback handling
- Mock LLM pipeline with delays to test report at different stages
"""

import json
import time
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest


# ---------------------------------------------------------------------------
# 1. NODE_STAGE_MAP completeness
# ---------------------------------------------------------------------------

class TestNodeStageMap:
    """Verify NODE_STAGE_MAP covers all graph nodes and is consistent."""

    def test_all_analyst_nodes_mapped(self):
        from tradingagents.batch_runner import NODE_STAGE_MAP
        expected_nodes = [
            "Market Analyst", "Sentiment Analyst", "News Analyst",
            "Fundamentals Analyst",
        ]
        for node in expected_nodes:
            assert node in NODE_STAGE_MAP, f"{node} missing from NODE_STAGE_MAP"

    def test_all_debate_nodes_mapped(self):
        from tradingagents.batch_runner import NODE_STAGE_MAP
        for node in ["Bull Researcher", "Bear Researcher", "Research Manager"]:
            assert node in NODE_STAGE_MAP, f"{node} missing from NODE_STAGE_MAP"

    def test_all_risk_nodes_mapped(self):
        from tradingagents.batch_runner import NODE_STAGE_MAP
        for node in ["Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"]:
            assert node in NODE_STAGE_MAP, f"{node} missing from NODE_STAGE_MAP"

    def test_trader_and_portfolio_mapped(self):
        from tradingagents.batch_runner import NODE_STAGE_MAP
        assert "Trader" in NODE_STAGE_MAP
        assert "Portfolio Manager" in NODE_STAGE_MAP

    def test_clear_and_tool_nodes_are_none(self):
        from tradingagents.batch_runner import NODE_STAGE_MAP
        # Clear and tool nodes should have None stage (not tracked)
        for node in ["Msg Clear Market", "tools_market", "Msg Clear Sentiment",
                      "tools_social", "Msg Clear News", "tools_news",
                      "Msg Clear Fundamentals", "tools_fundamentals"]:
            assert node in NODE_STAGE_MAP, f"{node} missing"
            stage, group = NODE_STAGE_MAP[node]
            assert stage is None, f"{node} should have None stage"
            assert group is None, f"{node} should have None group"

    def test_groups_are_valid(self):
        from tradingagents.batch_runner import NODE_STAGE_MAP, STAGE_ORDER
        valid_groups = set(STAGE_ORDER) | {None}
        for node, (stage, group) in NODE_STAGE_MAP.items():
            assert group in valid_groups, f"{node} has invalid group: {group}"

    def test_stage_order_is_complete(self):
        from tradingagents.batch_runner import STAGE_ORDER
        assert STAGE_ORDER == ["analysts", "research", "trading", "risk", "portfolio"]


# ---------------------------------------------------------------------------
# 2. Stage tracking helpers
# ---------------------------------------------------------------------------

class TestGetCompletedGroups:
    """Test _get_completed_groups correctly identifies completed pipeline stages."""

    def test_empty_state_returns_empty(self):
        from tradingagents.batch_runner import _get_completed_groups
        assert _get_completed_groups({}) == []

    def test_market_report_not_enough_for_analysts(self):
        """Single analyst report is NOT enough to mark analysts as complete."""
        from tradingagents.batch_runner import _get_completed_groups
        state = {"market_report": "## Market Report\n..."}
        completed = _get_completed_groups(state)
        assert "analysts" not in completed

    def test_all_four_reports_marks_analysts(self):
        from tradingagents.batch_runner import _get_completed_groups
        state = {
            "market_report": "report",
            "sentiment_report": "report",
            "news_report": "report",
            "fundamentals_report": "report",
        }
        completed = _get_completed_groups(state)
        assert "analysts" in completed

    def test_three_reports_not_enough_for_analysts(self):
        """Three analyst reports is NOT enough — need all 4."""
        from tradingagents.batch_runner import _get_completed_groups
        state = {
            "market_report": "report",
            "sentiment_report": "report",
            "news_report": "report",
        }
        completed = _get_completed_groups(state)
        assert "analysts" not in completed

    def test_bull_history_marks_research(self):
        from tradingagents.batch_runner import _get_completed_groups
        state = {"investment_debate_state": {"bull_history": "bull said..."}}
        completed = _get_completed_groups(state)
        assert "research" in completed

    def test_judge_decision_marks_research(self):
        from tradingagents.batch_runner import _get_completed_groups
        state = {"investment_debate_state": {"judge_decision": "buy"}}
        completed = _get_completed_groups(state)
        assert "research" in completed

    def test_trader_plan_marks_trading(self):
        from tradingagents.batch_runner import _get_completed_groups
        state = {"trader_investment_plan": "entry at 100"}
        completed = _get_completed_groups(state)
        assert "trading" in completed

    def test_risk_aggressive_marks_risk(self):
        from tradingagents.batch_runner import _get_completed_groups
        state = {"risk_debate_state": {"aggressive_history": "go for it"}}
        completed = _get_completed_groups(state)
        assert "risk" in completed

    def test_final_trade_decision_marks_portfolio(self):
        from tradingagents.batch_runner import _get_completed_groups
        state = {"final_trade_decision": "**Rating**: Buy"}
        completed = _get_completed_groups(state)
        assert "portfolio" in completed

    def test_full_pipeline_all_groups(self):
        from tradingagents.batch_runner import _get_completed_groups
        state = {
            "market_report": "m",
            "sentiment_report": "s",
            "news_report": "n",
            "fundamentals_report": "f",
            "investment_debate_state": {"judge_decision": "buy"},
            "trader_investment_plan": "entry at 100",
            "risk_debate_state": {"judge_decision": "proceed"},
            "final_trade_decision": "**Rating**: Buy",
        }
        completed = _get_completed_groups(state)
        assert set(completed) == {"analysts", "research", "trading", "risk", "portfolio"}


class TestCurrentStageName:
    """Test _current_stage_name returns the correct friendly name."""

    def test_nothing_done_returns_market_analyst(self):
        from tradingagents.batch_runner import _current_stage_name
        assert _current_stage_name([]) == "Market Analyst"

    def test_analysts_done_returns_research(self):
        from tradingagents.batch_runner import _current_stage_name
        assert _current_stage_name(["analysts"]) == "Bull/Bear Debate"

    def test_research_done_returns_trader(self):
        from tradingagents.batch_runner import _current_stage_name
        assert _current_stage_name(["analysts", "research"]) == "Trader"

    def test_trading_done_returns_risk(self):
        from tradingagents.batch_runner import _current_stage_name
        assert _current_stage_name(["analysts", "research", "trading"]) == "Risk Debate"

    def test_risk_done_returns_portfolio(self):
        from tradingagents.batch_runner import _current_stage_name
        groups = ["analysts", "research", "trading", "risk"]
        assert _current_stage_name(groups) == "Portfolio Manager"

    def test_all_done_returns_complete(self):
        from tradingagents.batch_runner import _current_stage_name
        groups = ["analysts", "research", "trading", "risk", "portfolio"]
        assert _current_stage_name(groups) == "Complete"


# ---------------------------------------------------------------------------
# 3. Partial state writing
# ---------------------------------------------------------------------------

class TestSavePartialState:
    """Test _save_partial_state writes correct JSON to disk."""

    def test_writes_non_empty_fields(self, tmp_path):
        from tradingagents.batch_runner import _save_partial_state
        config = {"results_dir": str(tmp_path)}
        state = {
            "market_report": "## Market Report",
            "sentiment_report": "",  # empty, should be excluded
            "news_report": "## News Report",
        }
        path = _save_partial_state("TEST.NS", state, config)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["market_report"] == "## Market Report"
        assert data["news_report"] == "## News Report"
        assert "sentiment_report" not in data

    def test_writes_investment_debate_state(self, tmp_path):
        from tradingagents.batch_runner import _save_partial_state
        config = {"results_dir": str(tmp_path)}
        state = {
            "investment_debate_state": {
                "bull_history": "bull says buy",
                "bear_history": "bear says sell",
                "judge_decision": "",
            }
        }
        path = _save_partial_state("TEST.NS", state, config)
        data = json.loads(path.read_text())
        assert data["investment_debate_state"]["bull_history"] == "bull says buy"

    def test_empty_state_writes_empty_json(self, tmp_path):
        from tradingagents.batch_runner import _save_partial_state
        config = {"results_dir": str(tmp_path)}
        path = _save_partial_state("TEST.NS", {}, config)
        data = json.loads(path.read_text())
        assert data == {}

    def test_creates_parent_directory(self, tmp_path):
        from tradingagents.batch_runner import _save_partial_state
        config = {"results_dir": str(tmp_path / "nonexistent" / "deep")}
        path = _save_partial_state("TEST.NS", {"market_report": "m"}, config)
        assert path.exists()


# ---------------------------------------------------------------------------
# 4. Partial report generation
# ---------------------------------------------------------------------------

class TestGenerateReportFile:
    """Test _generate_report_file creates correct markdown from partial state."""

    def test_generates_report_with_all_sections(self, tmp_path):
        """Simulate a complete pipeline and verify the report has all 5 sections."""
        from tradingagents.notifications.telegram_bot import _generate_report_file

        # Write partial state to the path the function expects
        partial = {
            "market_report": "## Market is bullish\nStrong uptrend.",
            "sentiment_report": "## Sentiment is positive\nScore: 0.7",
            "news_report": "## News overview\nGood quarter.",
            "fundamentals_report": "## Fundamentals\nP/E at 25.",
            "investment_debate_state": {
                "bull_history": "Bull: buy because growth",
                "bear_history": "Bear: sell because overvalued",
                "judge_decision": "Research says: Buy",
            },
            "trader_investment_plan": "**Entry Price**: 1000\n**Stop Loss**: 900",
            "risk_debate_state": {
                "aggressive_history": "Aggressive: go all in",
                "conservative_history": "Conservative: wait",
                "neutral_history": "Neutral: balanced",
                "judge_decision": "**Rating**: Buy\n**Price Target**: 1200",
            },
        }
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir(parents=True)
        partial_path = daily_dir / "TEST.NS_partial.json"
        partial_path.write_text(json.dumps(partial))

        stock = {
            "ticker": "TEST.NS",
            "status": "completed",
            "signal": "Buy",
            "completed_groups": ["analysts", "research", "trading", "risk", "portfolio"],
        }

        # Patch _results_dir to use tmp_path
        with patch("tradingagents.notifications.telegram_bot._results_dir", return_value=tmp_path):
            result = _generate_report_file("TEST.NS", stock)

        assert result is not None
        assert result.exists()
        content = result.read_text()

        # Verify all 5 sections present
        assert "## I. Analyst Team Reports" in content
        assert "### Market Analyst" in content
        assert "### Sentiment Analyst" in content
        assert "### News Analyst" in content
        assert "### Fundamentals Analyst" in content
        assert "## II. Research Team Decision" in content
        assert "### Bull Researcher" in content
        assert "### Bear Researcher" in content
        assert "### Research Manager" in content
        assert "## III. Trading Team Plan" in content
        assert "### Trader" in content
        assert "## IV. Risk Management Team Decision" in content
        assert "### Aggressive Analyst" in content
        assert "### Conservative Analyst" in content
        assert "### Neutral Analyst" in content
        assert "## V. Portfolio Manager Decision" in content
        assert "### Portfolio Manager" in content

        # Verify header
        assert "# Trading Analysis Report: TEST.NS" in content
        assert "Status: Completed" in content

        result.unlink(missing_ok=True)

    def test_generates_partial_report_with_only_analysts(self, tmp_path):
        """Simulate partial pipeline (only analysts done) and verify partial report."""
        from tradingagents.notifications.telegram_bot import _generate_report_file

        partial = {
            "market_report": "## Market Report\nBear regime.",
            "sentiment_report": "## Sentiment Report\nNeutral.",
        }
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir(parents=True)
        partial_path = daily_dir / "PARTIAL.NS_partial.json"
        partial_path.write_text(json.dumps(partial))

        stock = {
            "ticker": "PARTIAL.NS",
            "status": "in_progress",
            "completed_groups": ["analysts"],
        }

        with patch("tradingagents.notifications.telegram_bot._results_dir", return_value=tmp_path):
            result = _generate_report_file("PARTIAL.NS", stock)

        assert result is not None
        content = result.read_text()

        # Only analysts section should be present
        assert "## I. Analyst Team Reports" in content
        assert "### Market Analyst" in content
        assert "### Sentiment Analyst" in content
        assert "In Progress (1/5 stages complete)" in content

        # Other sections should NOT be present
        assert "## II. Research" not in content
        assert "## III. Trading" not in content
        assert "## IV. Risk" not in content
        assert "## V. Portfolio" not in content

        result.unlink(missing_ok=True)

    def test_returns_none_when_no_state(self, tmp_path):
        from tradingagents.notifications.telegram_bot import _generate_report_file
        stock = {"ticker": "EMPTY.NS", "status": "in_progress", "completed_groups": []}
        with patch("tradingagents.notifications.telegram_bot._results_dir", return_value=tmp_path):
            result = _generate_report_file("EMPTY.NS", stock)
        assert result is None

    def test_completed_stock_uses_complete_report_if_available(self, tmp_path):
        """For completed stocks, prefer the full report over partial state."""
        from tradingagents.notifications.telegram_bot import _generate_report_file

        # Create the complete report
        daily_dir = tmp_path / "daily" / "2026-09-06" / "DONE.NS"
        daily_dir.mkdir(parents=True)
        complete = daily_dir / "complete_report.md"
        complete.write_text("# Full Report\nThis is the complete report.")

        stock = {
            "ticker": "DONE.NS",
            "status": "completed",
            "completed_groups": ["analysts", "research", "trading", "risk", "portfolio"],
        }

        with patch("tradingagents.notifications.telegram_bot._results_dir", return_value=tmp_path):
            result = _generate_report_file("DONE.NS", stock)

        assert result is not None
        content = result.read_text()
        assert "This is the complete report." in content
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 5. Telegram bot report keyboard
# ---------------------------------------------------------------------------

class TestBuildReportKeyboard:
    """Test inline keyboard construction."""

    def test_completed_stock_shows_signal(self):
        from tradingagents.notifications.telegram_bot import _build_report_keyboard
        stocks = [{"ticker": "RELIANCE.NS", "status": "completed", "signal": "Buy"}]
        kb = json.loads(_build_report_keyboard(stocks))
        assert len(kb["inline_keyboard"]) == 1
        btn = kb["inline_keyboard"][0][0]
        assert "RELIANCE.NS" in btn["text"]
        assert "Buy" in btn["text"]
        assert btn["callback_data"] == "report:RELIANCE.NS"

    def test_in_progress_stock_shows_stage(self):
        from tradingagents.notifications.telegram_bot import _build_report_keyboard
        stocks = [{"ticker": "TCS.NS", "status": "in_progress", "stage": "Bull/Bear Debate"}]
        kb = json.loads(_build_report_keyboard(stocks))
        btn = kb["inline_keyboard"][0][0]
        assert "TCS.NS" in btn["text"]
        assert "Bull/Bear Debate" in btn["text"]

    def test_pending_stock_shows_pending(self):
        from tradingagents.notifications.telegram_bot import _build_report_keyboard
        stocks = [{"ticker": "INFY.NS", "status": "pending"}]
        kb = json.loads(_build_report_keyboard(stocks))
        btn = kb["inline_keyboard"][0][0]
        assert "INFY.NS" in btn["text"]

    def test_multiple_stocks(self):
        from tradingagents.notifications.telegram_bot import _build_report_keyboard
        stocks = [
            {"ticker": "A.NS", "status": "completed", "signal": "Hold"},
            {"ticker": "B.NS", "status": "in_progress", "stage": "Trader"},
            {"ticker": "C.NS", "status": "pending"},
        ]
        kb = json.loads(_build_report_keyboard(stocks))
        assert len(kb["inline_keyboard"]) == 3


# ---------------------------------------------------------------------------
# 6. Telegram bot stock list building
# ---------------------------------------------------------------------------

class TestGetStockList:
    """Test _get_stock_list builds correct ordered list from progress."""

    def test_empty_progress(self):
        from tradingagents.notifications.telegram_bot import _get_stock_list
        result = _get_stock_list({"analyzed": [], "stocks": {}})
        assert result == []

    def test_completed_stocks_in_order(self):
        from tradingagents.notifications.telegram_bot import _get_stock_list
        progress = {
            "analyzed": ["A.NS", "B.NS"],
            "stocks": {
                "A.NS": {"status": "completed", "signal": "Buy"},
                "B.NS": {"status": "completed", "signal": "Hold"},
            },
        }
        result = _get_stock_list(progress)
        assert len(result) == 2
        assert result[0]["ticker"] == "A.NS"
        assert result[1]["ticker"] == "B.NS"

    def test_in_progress_stock_after_completed(self):
        from tradingagents.notifications.telegram_bot import _get_stock_list
        progress = {
            "analyzed": ["A.NS"],
            "current_stock": "B.NS",
            "stocks": {
                "A.NS": {"status": "completed", "signal": "Buy"},
                "B.NS": {"status": "in_progress", "stage": "Trader"},
            },
        }
        result = _get_stock_list(progress)
        assert len(result) == 2
        assert result[0]["ticker"] == "A.NS"
        assert result[1]["ticker"] == "B.NS"
        assert result[1]["status"] == "in_progress"

    def test_pending_stocks_appended(self):
        from tradingagents.notifications.telegram_bot import _get_stock_list
        progress = {
            "analyzed": ["A.NS"],
            "current_stock": "B.NS",
            "total_stocks": 4,
            "stocks": {
                "A.NS": {"status": "completed"},
                "B.NS": {"status": "in_progress"},
            },
        }
        result = _get_stock_list(progress)
        assert len(result) == 4
        assert result[2]["status"] == "pending"
        assert result[3]["status"] == "pending"

    def test_no_duplicate_when_current_in_analyzed(self):
        from tradingagents.notifications.telegram_bot import _get_stock_list
        progress = {
            "analyzed": ["A.NS"],
            "current_stock": "A.NS",  # already completed
            "stocks": {"A.NS": {"status": "completed"}},
        }
        result = _get_stock_list(progress)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# 7. Mock LLM pipeline test — report at different stages
# ---------------------------------------------------------------------------

class TestMockPipelineReportAtDifferentStages:
    """Mock the full LLM pipeline with delays and verify partial reports
    are generated correctly at each stage of completion."""

    @pytest.fixture
    def mock_graph_chunks(self):
        """Simulate graph stream chunks with delays between stages."""
        import asyncio

        # Simulate the sequence of chunks from graph.stream()
        # Each chunk is {node_name: state_update}
        chunks = [
            # Stage 1: Market Analyst
            ({"market_report": "## Market Analysis\nBEAR regime detected. Confidence: 55%.\n\nKey indicators:\n- NIFTY 50 below 200 DMA\n- India VIX elevated at 14.5\n- FII outflows continuing"}, "Market Analyst"),
            ({"messages": []}, "Msg Clear Market"),
            ({"messages": []}, "tools_market"),
            # Stage 2: Sentiment Analyst
            ({"sentiment_report": "## Sentiment Analysis\nOverall Score: 0.25 (Slightly Bearish)\n\nSocial sentiment mixed:\n- StockTwits: 45% bullish\n- Reddit: cautious tone\n- News sentiment: neutral"}, "Sentiment Analyst"),
            ({"messages": []}, "Msg Clear Sentiment"),
            ({"messages": []}, "tools_social"),
            # Stage 3: News Analyst
            ({"news_report": "## News Analysis\nSentiment Score: 0.3\nRelevance: 0.7\n\nRecent headlines:\n- Q3 results below expectations\n- New factory expansion announced\n- Regulatory concerns"}, "News Analyst"),
            ({"messages": []}, "Msg Clear News"),
            ({"messages": []}, "tools_news"),
            # Stage 4: Fundamentals Analyst
            ({"fundamentals_report": "## Fundamentals\nSector: Consumer Non-Cyclicals\nP/E: 52.3 (premium)\nROE: 18.5%\nDebt/Equity: 0.3\n\nValuation: Fair"}, "Fundamentals Analyst"),
            ({"messages": []}, "Msg Clear Fundamentals"),
            ({"messages": []}, "tools_fundamentals"),
            # Stage 5: Research - Bull/Bear Debate
            ({"investment_debate_state": {"bull_history": "Bull: Strong brand, market leader, pricing power. Growth in decorative segment.", "bear_history": "", "history": "", "current_response": "", "judge_decision": "", "count": 0}}, "Bull Researcher"),
            ({"investment_debate_state": {"bull_history": "Bull: Strong brand, market leader, pricing power.", "bear_history": "Bear: Overvalued at 52x P/E, margin pressure from raw materials, slow urban demand.", "history": "", "current_response": "", "judge_decision": "", "count": 1}}, "Bear Researcher"),
            ({"investment_debate_state": {"bull_history": "Bull: Strong brand.", "bear_history": "Bear: Overvalued.", "history": "", "current_response": "", "judge_decision": "Research Decision: The bull case has merit in the long term, but near-term headwinds from raw material costs and slow demand are real. Recommend HOLD with accumulation on dips.", "count": 2}}, "Research Manager"),
            # Stage 6: Trader
            ({"trader_investment_plan": "**Entry Price**: ₹7,382.55\n**Stop Loss**: ₹6,800.00\n**Target Price**: ₹8,400.00\n**Position Sizing**: 5% of portfolio\n**Time Horizon**: 3-6 months"}, "Trader"),
            # Stage 7: Risk Debate
            ({"risk_debate_state": {"aggressive_history": "Aggressive: Buy now. Brand moat is strong.", "conservative_history": "", "neutral_history": "", "latest_speaker": "", "current_aggressive_response": "", "current_conservative_response": "", "current_neutral_response": "", "judge_decision": "", "count": 0}}, "Aggressive Analyst"),
            ({"risk_debate_state": {"aggressive_history": "Aggressive: Buy now.", "conservative_history": "Conservative: Wait for Q4 results. Raw material risk is real.", "neutral_history": "", "latest_speaker": "", "current_aggressive_response": "", "current_conservative_response": "", "current_neutral_response": "", "judge_decision": "", "count": 1}}, "Conservative Analyst"),
            ({"risk_debate_state": {"aggressive_history": "Aggressive: Buy.", "conservative_history": "Conservative: Wait.", "neutral_history": "Neutral: Balanced approach. Start with 2.5% position, add on dips.", "latest_speaker": "", "current_aggressive_response": "", "current_conservative_response": "", "current_neutral_response": "", "judge_decision": "", "count": 2}}, "Neutral Analyst"),
            ({"risk_debate_state": {"aggressive_history": "Aggressive: Buy.", "conservative_history": "Conservative: Wait.", "neutral_history": "Neutral: Balanced.", "latest_speaker": "", "current_aggressive_response": "", "current_conservative_response": "", "current_neutral_response": "", "judge_decision": "**Rating**: Buy\n**Price Target**: ₹8,400.00\n**Time Horizon**: 3-6 months\n\n**Executive Summary**: Despite near-term headwinds, strong brand moat and market position justify a Buy.\n\n**Investment Thesis**: Accumulate on dips.", "count": 3}}, "Portfolio Manager"),
            # Final trade decision
            ({"final_trade_decision": "**Rating**: Buy\n**Price Target**: ₹8,400.00\n**Entry Price**: ₹7,382.55\n**Stop Loss**: ₹6,800.00\n**Time Horizon**: 3-6 months\n\n**Executive Summary**: Strong brand moat.\n\n**Investment Thesis**: Accumulate on dips."}, None),
        ]
        return chunks

    def test_partial_state_after_market_analyst(self, mock_graph_chunks):
        """After market analyst, only market_report should be present."""
        from tradingagents.batch_runner import _get_completed_groups

        partial_state = {}
        for chunk, node_name in mock_graph_chunks[:1]:  # Only first chunk
            for key, val in chunk.items():
                if val is not None:
                    partial_state[key] = val

        assert partial_state.get("market_report") is not None
        assert partial_state.get("sentiment_report") is None
        completed = _get_completed_groups(partial_state)
        # With new logic: analysts requires ALL 4 reports, so not completed yet
        assert "analysts" not in completed

    def test_partial_state_after_all_analysts(self, mock_graph_chunks):
        """After all 4 analysts, analysts group should be completed."""
        from tradingagents.batch_runner import _get_completed_groups

        # Take chunks up to and including Fundamentals Analyst
        analyst_chunks = mock_graph_chunks[:12]  # Up to Fundamentals Analyst
        partial_state = {}
        for chunk, node_name in analyst_chunks:
            for key, val in chunk.items():
                if val is not None:
                    partial_state[key] = val

        assert partial_state.get("market_report") is not None
        assert partial_state.get("sentiment_report") is not None
        assert partial_state.get("news_report") is not None
        assert partial_state.get("fundamentals_report") is not None
        completed = _get_completed_groups(partial_state)
        assert "analysts" in completed
        assert "research" not in completed

    def test_partial_state_after_research(self, mock_graph_chunks):
        """After research debate, analysts + research should be completed."""
        from tradingagents.batch_runner import _get_completed_groups

        # Take chunks up to Research Manager
        research_chunks = mock_graph_chunks[:15]
        partial_state = {}
        for chunk, node_name in research_chunks:
            for key, val in chunk.items():
                if val is not None:
                    partial_state[key] = val

        completed = _get_completed_groups(partial_state)
        assert "analysts" in completed
        assert "research" in completed
        assert "trading" not in completed

    def test_partial_state_after_trader(self, mock_graph_chunks):
        """After trader, analysts + research + trading should be completed."""
        from tradingagents.batch_runner import _get_completed_groups

        # Take chunks up to Trader
        trader_chunks = mock_graph_chunks[:16]
        partial_state = {}
        for chunk, node_name in trader_chunks:
            for key, val in chunk.items():
                if val is not None:
                    partial_state[key] = val

        completed = _get_completed_groups(partial_state)
        assert "analysts" in completed
        assert "research" in completed
        assert "trading" in completed
        assert "risk" not in completed

    def test_full_pipeline_all_groups_completed(self, mock_graph_chunks):
        """After full pipeline, all 5 groups should be completed."""
        from tradingagents.batch_runner import _get_completed_groups

        partial_state = {}
        for chunk, node_name in mock_graph_chunks:
            for key, val in chunk.items():
                if val is not None:
                    partial_state[key] = val

        completed = _get_completed_groups(partial_state)
        assert set(completed) == {"analysts", "research", "trading", "risk", "portfolio"}

    def test_partial_report_at_each_stage(self, mock_graph_chunks, tmp_path):
        """Verify partial report file at each pipeline stage has correct sections."""
        from tradingagents.notifications.telegram_bot import _generate_report_file
        from tradingagents.batch_runner import _get_completed_groups, STAGE_ORDER

        # chunk_idx is the slice end index (exclusive)
        # After market analyst (chunk 0): 1 chunk total
        # After all analysts (chunks 0-11): 12 chunks
        # After research (chunks 0-14): 15 chunks
        # After trader (chunks 0-15): 16 chunks
        # After risk debate (chunks 0-18): 19 chunks
        # After portfolio (chunks 0-20): 21 chunks
        stage_checkpoints = [
            (12, ["analysts"], ["## I. Analyst Team Reports"]),
            (15, ["analysts", "research"], ["## I. Analyst Team Reports", "## II. Research Team Decision"]),
            (16, ["analysts", "research", "trading"],
             ["## I. Analyst Team Reports", "## II. Research Team Decision", "## III. Trading Team Plan"]),
            (19, ["analysts", "research", "trading", "risk"],
             ["## I.", "## II.", "## III.", "## IV."]),
            (21, ["analysts", "research", "trading", "risk", "portfolio"],
             ["## I.", "## II.", "## III.", "## IV.", "## V."]),
        ]

        for chunk_idx, expected_groups, expected_sections in stage_checkpoints:
            partial_state = {}
            for chunk, node_name in mock_graph_chunks[:chunk_idx]:
                for key, val in chunk.items():
                    if val is not None:
                        partial_state[key] = val

            # Write partial state to the path the function expects
            daily_dir = tmp_path / "daily"
            daily_dir.mkdir(parents=True, exist_ok=True)
            partial_path = daily_dir / "TEST.NS_partial.json"
            serializable = {}
            for key in ("market_report", "sentiment_report", "news_report",
                        "fundamentals_report", "investment_debate_state",
                        "trader_investment_plan", "risk_debate_state",
                        "final_trade_decision"):
                val = partial_state.get(key)
                if val:
                    serializable[key] = val
            partial_path.write_text(json.dumps(serializable))

            completed = _get_completed_groups(partial_state)
            stock = {
                "ticker": "TEST.NS",
                "status": "completed" if len(completed) == 5 else "in_progress",
                "completed_groups": completed,
            }

            with patch("tradingagents.notifications.telegram_bot._results_dir", return_value=tmp_path):
                result = _generate_report_file("TEST.NS", stock)

            assert result is not None, f"No report at chunk_idx={chunk_idx}"
            content = result.read_text()

            for section in expected_sections:
                assert section in content, (
                    f"Missing '{section}' at chunk_idx={chunk_idx}\n"
                    f"Content preview: {content[:500]}"
                )

            result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 8. stream_propagate yields correct structure
# ---------------------------------------------------------------------------

class TestStreamPropagate:
    """Test that stream_propagate yields (chunk, node_name) tuples."""

    def test_stream_propagate_exists(self):
        """Verify the method exists on TradingAgentsGraph."""
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        assert hasattr(TradingAgentsGraph, "stream_propagate")

    def test_stream_propagate_yields_tuples(self):
        """Verify stream_propagate yields (dict, str) tuples with a mock graph."""
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        g = object.__new__(TradingAgentsGraph)
        g.ticker = None
        g._resolve_pending_entries = MagicMock()
        g.memory_log = MagicMock()
        g.memory_log.get_past_context.return_value = ""
        g.resolve_instrument_context = MagicMock(return_value="TEST.NS")
        g.propagator = MagicMock()
        g.propagator.create_initial_state.return_value = {"messages": []}
        g.propagator.get_graph_args.return_value = {}
        g.checkpoint_scope = MagicMock()
        g.checkpoint_scope.__enter__ = MagicMock(return_value=None)
        g.checkpoint_scope.__exit__ = MagicMock(return_value=False)
        g.checkpoint_input = MagicMock(return_value={"messages": []})
        g._memory_as_of = MagicMock(return_value=None)

        # Mock graph.stream to yield chunks
        mock_graph = MagicMock()
        mock_graph.stream.return_value = [
            {"Market Analyst": {"market_report": "market report"}},
            {"Sentiment Analyst": {"sentiment_report": "sentiment report"}},
        ]
        g.graph = mock_graph

        results = list(g.stream_propagate("TEST.NS", "2026-09-06"))

        assert len(results) == 2
        # Each result is (chunk, node_name)
        chunk1, name1 = results[0]
        assert isinstance(chunk1, dict)
        assert name1 == "Market Analyst"
        assert chunk1["Market Analyst"]["market_report"] == "market report"

        chunk2, name2 = results[1]
        assert name2 == "Sentiment Analyst"
        assert chunk2["Sentiment Analyst"]["sentiment_report"] == "sentiment report"


# ---------------------------------------------------------------------------
# 9. Telegram bot callback handling
# ---------------------------------------------------------------------------

class TestHandleCallback:
    """Test _handle_callback processes button taps correctly."""

    def test_callback_answers_query(self):
        from tradingagents.notifications.telegram_bot import _handle_callback

        with patch("tradingagents.notifications.telegram.answer_callback") as mock_answer, \
             patch("tradingagents.notifications.telegram_bot._load_progress") as mock_load, \
             patch("tradingagents.notifications.telegram_bot._get_stock_list") as mock_list, \
             patch("tradingagents.notifications.telegram_bot._send_stock_report") as mock_send:

            mock_load.return_value = {"analyzed": [], "stocks": {}}
            mock_list.return_value = [
                {"ticker": "TEST.NS", "status": "completed", "signal": "Buy"}
            ]

            callback = {
                "id": "12345",
                "data": "report:TEST.NS",
                "message": {"chat": {"id": "6649867665"}},
            }

            _handle_callback("token", "6649867665", callback)

            mock_answer.assert_called_once_with("12345")
            mock_send.assert_called_once()
            # Verify the stock passed to _send_stock_report
            call_args = mock_send.call_args
            assert call_args[0][2]["ticker"] == "TEST.NS"

    def test_callback_unknown_ticker(self):
        from tradingagents.notifications.telegram_bot import _handle_callback

        with patch("tradingagents.notifications.telegram.answer_callback"), \
             patch("tradingagents.notifications.telegram_bot._load_progress") as mock_load, \
             patch("tradingagents.notifications.telegram_bot._get_stock_list") as mock_list, \
             patch("tradingagents.notifications.telegram_bot._send") as mock_send:

            mock_load.return_value = {}
            mock_list.return_value = [{"ticker": "A.NS", "status": "completed"}]

            callback = {
                "id": "99999",
                "data": "report:NONEXISTENT.NS",
                "message": {"chat": {"id": "6649867665"}},
            }

            _handle_callback("token", "6649867665", callback)
            mock_send.assert_called_once()
            assert "NONEXISTENT.NS" in mock_send.call_args[0][2]


# ---------------------------------------------------------------------------
# 10. Integration: progress file schema
# ---------------------------------------------------------------------------

class TestProgressFileSchema:
    """Verify the progress file has the expected schema with stocks field."""

    def test_load_progress_returns_stocks_field(self):
        from tradingagents.notifications.telegram_bot import _load_progress
        # Empty progress should have stocks or handle missing gracefully
        progress = {"analyzed": [], "signals": [], "errors": [], "started_at": None}
        # _get_stock_list should handle missing 'stocks' key
        from tradingagents.notifications.telegram_bot import _get_stock_list
        result = _get_stock_list(progress)
        assert isinstance(result, list)

    def test_progress_with_all_fields(self, tmp_path):
        """Test that batch_runner progress structure is compatible with bot reading."""
        from tradingagents.batch_runner import _save_progress, _load_progress

        progress = {
            "analyzed": ["A.NS", "B.NS"],
            "signals": [{"ticker": "A.NS", "signal": "Buy"}],
            "errors": [],
            "started_at": "2026-09-06T09:00:00",
            "completed_at": "2026-09-06T10:30:00",
            "total_stocks": 2,  # Match analyzed count so no pending entries
            "current_stock": None,
            "current_index": None,
            "stocks": {
                "A.NS": {
                    "status": "completed",
                    "signal": "Buy",
                    "stage": "Complete",
                    "completed_groups": ["analysts", "research", "trading", "risk", "portfolio"],
                    "elapsed_seconds": 45.2,
                    "report_path": "/some/path",
                },
                "B.NS": {
                    "status": "completed",
                    "signal": "Hold",
                    "stage": "Complete",
                    "completed_groups": ["analysts", "research", "trading", "risk", "portfolio"],
                    "elapsed_seconds": 38.1,
                },
            },
        }

        progress_path = tmp_path / "2026-09-06_progress.json"
        _save_progress(progress_path, progress)
        loaded = _load_progress(progress_path)

        assert loaded["total_stocks"] == 2
        assert loaded["stocks"]["A.NS"]["status"] == "completed"
        assert loaded["stocks"]["A.NS"]["signal"] == "Buy"
        assert len(loaded["stocks"]["A.NS"]["completed_groups"]) == 5

        # Bot should be able to read this
        from tradingagents.notifications.telegram_bot import _get_stock_list
        stock_list = _get_stock_list(loaded)
        assert len(stock_list) == 2
