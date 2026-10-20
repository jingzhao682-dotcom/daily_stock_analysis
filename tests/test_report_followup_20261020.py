"""Offline regressions from the 16:43 report: no model, mail or live quotes."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.analyzer import AnalysisResult
from src.config import Config
from src.core.pipeline import StockAnalysisPipeline
from src.enums import ReportType
from src.notification import NotificationService
from src.services.filing_evidence import quarantine_unverified_financials
from src.services.report_evidence_guard import redact_unverified_flow_lines


@pytest.mark.parametrize("claim", [
    "量比仅0.63，缺乏主力资金介入迹象", "有部分抄底盘", "缺乏主动性买盘承接",
])
@pytest.mark.parametrize("context", [None, {}, {"capital_flow": {"status": "not_supported", "data": {}}}])
def test_missing_flow_evidence_cannot_be_inferred_from_volume(claim, context):
    result = SimpleNamespace(fundamental_context=context)
    text = redact_unverified_flow_lines("日量比0.63\n" + claim, [result])
    assert claim not in text
    assert "日量比0.63" in text
    assert "已隐藏" in text


def test_growth_only_payload_still_requires_filing_reference():
    context = {"growth": {"status": "ok", "data": {"roe": 117.21, "revenue_yoy": 105.85}}}
    guarded = quarantine_unverified_financials(context, market="us")
    assert guarded["growth"]["data"] == {}
    assert context["growth"]["data"]["roe"] == 117.21
    assert quarantine_unverified_financials(context, market="cn") is context


@pytest.mark.parametrize("renderer", [False, True])
@pytest.mark.parametrize("turnover", ["N/A", "0.00%", "1.25%"])
def test_real_dashboard_uses_provider_turnover_in_both_renderers(renderer, turnover):
    config = Config(stock_list=[], report_renderer_enabled=renderer, report_language="zh")
    result = AnalysisResult(code="NVDA", name="NVDA", sentiment_score=45,
                            trend_prediction="mixed", operation_advice="观望",
                            market_snapshot={"turnover_rate": turnover},
                            dashboard={"data_perspective": {"volume_analysis": {
                                "volume_ratio": 0.943, "turnover_rate": 0,
                                "volume_status": "平量", "volume_meaning": "日量观察",
                            }}})
    with patch("src.notification.get_config", return_value=config), patch(
        "src.services.report_renderer.get_config", return_value=config
    ):
        service = NotificationService()
        report = service.generate_dashboard_report([result], report_date="2026-10-20")
    assert "换手率 " + turnover in report
    assert "换手率 0%" not in report
    assert "N/A%" not in report


def test_legacy_context_is_gated_again_at_financial_render_boundary():
    service = NotificationService.__new__(NotificationService)
    result = AnalysisResult(code="NVDA", name="NVDA", sentiment_score=45,
                            trend_prediction="mixed", operation_advice="观望")
    result.fundamental_context = {
        "earnings": {"data": {"financial_report": {"report_date": "2026-07-31"},
                              "dividend": {"ttm_event_count": 4}}},
        "growth": {"data": {"roe": 117.21, "gross_margin": 74.67}},
    }
    lines = []
    service._append_fundamental_blocks(lines, result)
    text = "\n".join(lines)
    assert "117.21" not in text and "74.67" not in text
    assert "缺少 SEC" in text
    assert "股东回报" in text


def test_saved_and_sent_aggregate_common_path_discloses_failed_symbols():
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.notifier = SimpleNamespace(generate_aggregate_report=Mock(return_value="# report"))
    pipeline._last_batch_coverage = {
        "requested": ["SGOV", "NVDA", "DRAM", "SLV", "SPCX"],
        "failed": ["DRAM", "SLV", "SPCX"],
    }
    text = pipeline._generate_aggregate_report([], ReportType.SIMPLE)
    assert "请求 5 只，成功 2 只，失败 3 只" in text
    assert "DRAM, SLV, SPCX" in text
    assert "失败不代表剔除或卖出" in text
    assert text.endswith("# report")


@pytest.mark.parametrize("override", [
    {"currency": "CNY"}, {"symbol": "GLD"}, {"price": 0},
    {"price": float("nan")}, {"updatedAt": "2026-10-20T08:00:00"},
    {"updatedAt": "2000-01-01T00:00:00Z"}, {"updatedAt": "2099-01-01T00:00:00Z"},
])
def test_invalid_live_gold_never_becomes_a_verified_quote(override):
    import io
    import json
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from src.services.gold_benchmarks import build_gold_benchmark_section

    payload = {"currency": "USD", "symbol": "XAU", "price": 4121.60,
               "updatedAt": datetime.now(timezone.utc).isoformat(), **override}
    response = MagicMock()
    response.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
    with patch("urllib.request.urlopen", return_value=response), patch(
        "yfinance.Ticker", side_effect=RuntimeError("unavailable")
    ), patch("pandas.read_csv", side_effect=RuntimeError("unavailable")), patch(
        "akshare.futures_main_sina", side_effect=RuntimeError("unavailable")
    ):
        report = build_gold_benchmark_section()
    assert report.count("数据缺失") == 2
    assert "4121.60" not in report
