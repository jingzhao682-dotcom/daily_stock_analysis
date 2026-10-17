"""Regression cases from the mixed-basis US daily reports; no network or mail."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.analyzer import AnalysisResult, GeminiAnalyzer
from src.core.market_profile import get_profile
from src.core.market_strategy import get_market_strategy_blueprint
from src.market_analyzer import MarketAnalyzer, MarketIndex, MarketOverview
from src.notification import NotificationService
from src.phase_decision_guardrail import apply_phase_decision_guardrails
from src.schemas.decision_action import display_action_fields_for_result


def analyzer(region="us", language="zh"):
    value = MarketAnalyzer.__new__(MarketAnalyzer)
    value.region = region
    value.profile = get_profile(region)
    value.strategy = get_market_strategy_blueprint(region)
    value.config = SimpleNamespace(report_language=language)
    return value


def overview(vix="VIX", vix_change=21.45):
    return MarketOverview(date="2026-10-16", indices=[
        MarketIndex(code="SPX", name="S&P 500", current=7659.44, change_pct=-1.09),
        MarketIndex(code="IXIC", name="Nasdaq", current=26140.03, change_pct=-2.15),
        MarketIndex(code="DJI", name="Dow", current=52002.41, change_pct=-0.34),
        MarketIndex(code=vix, name="VIX", current=19, change_pct=vix_change),
    ])


@pytest.mark.parametrize("vix", ["VIX", "^VIX", " vix "])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_equity_returns_and_explanation_exclude_volatility(vix, language):
    ma = analyzer(language=language)
    snapshot = ma.build_market_light_snapshot(overview(vix))
    assert snapshot["score"] == 45
    assert snapshot["dimensions"]["index"]["score"] == 35
    assert snapshot["data_quality"] == "partial"
    assert snapshot["status"] != "green"
    assert "-1.19%" in " ".join(snapshot["reasons"])
    assert "+4.47%" not in " ".join(snapshot["reasons"])
    # A rising or falling VIX cannot reverse equity return attribution.
    assert ma.build_market_light_snapshot(overview(vix, -90))["score"] == 45


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), "bad"])
def test_bad_equity_observations_and_vix_only_are_unavailable(bad):
    ma = analyzer()
    data = MarketOverview(date="2026-10-16", indices=[
        MarketIndex(code="VIX", name="Volatility", current=19, change_pct=21.45),
        MarketIndex(code="SPX", name="S&P", current=7000, change_pct=bad),
    ])
    snapshot = ma.build_market_light_snapshot(data)
    assert snapshot["data_quality"] == "unavailable"
    assert snapshot["status"] == "yellow"
    assert snapshot["dimensions"]["index"]["available"] is False


def test_duplicate_alias_does_not_reweight_equity_average():
    ma = analyzer()
    data = overview()
    expected = ma.build_market_light_snapshot(data)["score"]
    data.indices.append(MarketIndex(code="^GSPC", name="S&P", current=7659.44, change_pct=-1.09))
    assert ma.build_market_light_snapshot(data)["score"] == expected


def test_partial_rally_does_not_authorize_attack():
    ma = analyzer()
    data = MarketOverview(date="2026-10-16", indices=[
        MarketIndex(code="SPX", name="S&P", current=7000, change_pct=5),
    ])
    assert ma.build_market_light_snapshot(data)["status"] == "yellow"
    assert "长期" in ma.build_market_light_snapshot(data)["guidance"]


def test_us_limited_data_uses_one_deterministic_report_without_cash_target():
    ma = analyzer()
    ma.analyzer = Mock()
    ma._get_analyzer_generation_backend_config_error = Mock(return_value=None)
    report = ma.generate_market_review(overview(), [])
    assert "45/100" in report
    assert "-1.19%" in report
    assert "可进攻" not in report
    assert "70%" not in report and "80%" not in report
    assert "暂无新闻证据" in report
    assert "长期配置" in report
    ma.analyzer.generate_text.assert_not_called()


def test_market_overview_uses_provider_session_date_not_report_day():
    ma = analyzer()
    data = overview()
    for item in data.indices:
        item.trade_date = "2026-10-16"
    ma._get_main_indices = Mock(return_value=data.indices)
    assert ma.get_market_overview().date == "2026-10-16"


def test_mixed_quote_dates_are_not_presented_as_one_known_session():
    ma = analyzer()
    data = overview()
    data.indices[0].trade_date = "2026-10-16"
    data.indices[1].trade_date = "2026-10-15"
    ma._get_main_indices = Mock(return_value=data.indices)
    from unittest.mock import patch
    with patch("src.market_analyzer.datetime") as now:
        now.now.return_value.strftime.return_value = "2026-10-17"
        assert ma.get_market_overview().date == "2026-10-17"
    assert ma.build_market_light_snapshot(data)["data_quality"] == "unavailable"


def snapshot(context):
    return GeminiAnalyzer.__new__(GeminiAnalyzer)._build_market_snapshot(context)


def context(close=382.01, prev=382.71, pct=0.27):
    return {"code": "GLD", "date": "2026-10-17", "today": {
        "date": "2026-10-16", "close": close, "high": close + 1, "low": close - 1,
        "pct_chg": pct, "data_source": "yfinance",
    }, "yesterday": {"close": prev}}


def test_price_pair_discloses_conflict_without_mutating_provider_return():
    raw = context()
    original = deepcopy(raw)
    value = snapshot(raw)
    assert raw == original
    assert value["pct_chg"] == "-0.18%"
    assert value["change_amount"] == "-0.70"
    assert value["reported_pct_chg"] == "0.27%"
    assert value["data_quality"] == "partial"
    assert value["date"] == "2026-10-16"
    assert value["report_date"] == "2026-10-17"
    assert value["currency"] == "USD"
    assert value["consistency_note"]


def test_us_amount_has_usd_unit_instead_of_yuan():
    raw = context()
    raw["today"]["amount"] = 10000
    value = snapshot(raw)
    assert value["amount"] == "10.00K USD"


def test_live_quote_uses_own_prior_close_instead_of_adjusted_history():
    raw = context(close=100, prev=98, pct=0)
    raw["today"].update(data_source="realtime:yfinance", pre_close=100)
    value = snapshot(raw)
    assert value["prev_close"] == "100.00"
    assert value["pct_chg"] == "0.00%"
    assert value["data_quality"] == "ok"


def test_weekend_report_keeps_exchange_quote_date_and_unknown_is_not_guessed():
    raw = context(close=100, prev=98, pct=0)
    raw["today"].update(data_source="realtime:yfinance", pre_close=100,
                        date="2026-10-17", provider_timestamp="2026-10-16T20:00:00Z")
    assert snapshot(raw)["date"] == "2026-10-16"
    raw["today"].pop("provider_timestamp")
    assert snapshot(raw)["date"] == "N/A"


@pytest.mark.parametrize("prior", [None, 0, -1, float("nan"), float("inf")])
def test_live_missing_baseline_never_borrows_adjusted_history(prior):
    raw = context(close=100, prev=98, pct=0)
    raw["today"].update(data_source="realtime:yfinance", pre_close=prior)
    value = snapshot(raw)
    assert value["prev_close"] == "N/A"
    assert value["pct_chg"] == "N/A"
    assert value["change_amount"] == "N/A"
    assert value["data_quality"] == "partial"


@pytest.mark.parametrize("pct", [-0.18, -0.183, None])
def test_rounding_or_missing_provider_return_is_not_a_conflict(pct):
    assert snapshot(context(pct=pct))["data_quality"] == "ok"


@pytest.mark.parametrize("status", ["fallback", "partial", "missing", "stale", "fetch_failed", "estimated"])
@pytest.mark.parametrize("action", ["buy", "sell", "reduce", "add"])
def test_degraded_data_clears_all_action_surfaces_and_preserves_watch_badge(status, action):
    result = AnalysisResult(
        code="QQQM", name="ETF", sentiment_score=5 if action in {"sell", "reduce"} else 95,
        trend_prediction="weak", operation_advice=action, decision_type="sell", action=action,
        confidence_level="高", dashboard={
            "battle_plan": {"sniper_points": {"stop_loss": "sell now"}},
            "core_conclusion": {"position_advice": {"has_position": "sell now"}},
            "signal_attribution": {"news_sentiment": 18},
        },
    )
    changes = apply_phase_decision_guardrails(
        result, market_phase_summary=None,
        analysis_context_pack_overview={"blocks": [{"key": "technical", "status": status}]},
    )
    assert "action_downgraded_core_data_degraded" in changes
    assert result.decision_type == "hold"
    assert result.action == "watch"
    from src.core.pipeline import StockAnalysisPipeline
    StockAnalysisPipeline._refresh_decision_action_for_final_result(
        result, report_type="detailed", previous_operation_advice=action,
    )
    assert display_action_fields_for_result(result)["action"] == "watch"
    assert "长期配置" in result.get_position_advice(True)
    assert result.get_sniper_points() == {}
    assert "signal_attribution" not in result.dashboard
    assert result.confidence_level == "中"


def test_snapshot_conflict_degrades_even_without_context_pack_and_renders_disclosure():
    value = snapshot(context())
    result = AnalysisResult(code="GLD", name="GLD", sentiment_score=5,
                            trend_prediction="weak", operation_advice="卖出", decision_type="sell",
                            market_snapshot=value)
    apply_phase_decision_guardrails(result, market_phase_summary=None, analysis_context_pack_overview=None)
    assert result.action == "watch"
    service = NotificationService.__new__(NotificationService)
    lines = []
    service._append_market_snapshot(lines, result)
    rendered = "\n".join(lines)
    assert "USD" in rendered
    assert "2026-10-16" in rendered
    assert "-0.18%" in rendered
    assert value["consistency_note"] in rendered


def test_absent_news_does_not_become_a_factual_catalyst():
    result = AnalysisResult(code="NVDA", name="NVDA", sentiment_score=50,
                            trend_prediction="mixed", operation_advice="hold",
                            news_summary="An unverified earnings warning",
                            dashboard={"intelligence": {"latest_news": "unverified", "positive_catalysts": ["rumor"]}})
    apply_phase_decision_guardrails(result, market_phase_summary=None, analysis_context_pack_overview=None)
    assert "暂无新闻证据" in result.news_summary
    assert result.dashboard["intelligence"]["positive_catalysts"] == []
