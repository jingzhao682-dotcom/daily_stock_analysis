# -*- coding: utf-8 -*-
"""P1 tests: independent market benchmarks and fail-closed evidence rules."""
from unittest.mock import patch
import pandas as pd
from src.services.filing_evidence import audit_filing_reference
from src.services.gold_benchmarks import build_gold_benchmark_section
from src.stock_analyzer import StockTrendAnalyzer


def test_filing_requires_official_url_and_date():
    assert not audit_filing_reference({"report_date": "2026-06-30", "revenue": 123})["verified"]
    assert not audit_filing_reference({"report_date": "2026-06-30", "source_url": "http://www.sec.gov/x"})["verified"]
    assert not audit_filing_reference({"report_date": "2026-06-30", "source_url": "https://sec.gov.evil.com/x"})["verified"]
    assert audit_filing_reference({"report_date": "2026-06-30", "source_url": "https://www.sec.gov/Archives/edgar/data/1/a"})["verified"]


def test_gold_benchmarks_fail_closed_with_explicit_units():
    with patch("yfinance.Ticker", side_effect=Exception("provider blocked")), patch(
        "akshare.futures_main_sina", side_effect=Exception("provider blocked")
    ), patch("pandas.read_csv", side_effect=Exception("provider blocked")), patch(
        "urllib.request.urlopen", side_effect=Exception("provider blocked")
    ):
        report = build_gold_benchmark_section()
    assert "国际黄金现货" in report
    assert "上海期货交易所沪金主力" in report
    assert "美元/金衡盎司" in report
    assert "人民币/克" in report
    assert report.count("数据缺失") == 2
    assert "GLD/GLDM" in report


def test_daily_volume_ratio_uses_five_prior_trading_days():
    analysis = StockTrendAnalyzer()
    result = analysis.analyze(_bars(), "TEST")
    assert result.volume_ratio_5d == 2.0
    assert "前5交易日均值" in result.volume_trend
    assert "资金方向无法据此判断" in result.volume_trend


def test_missing_volume_does_not_claim_shrinkage():
    df = _bars()
    df.loc[df.index[-1], "volume"] = None
    result = StockTrendAnalyzer().analyze(df, "TEST")
    assert result.volume_ratio_5d == 0.0
    assert "无法判断量能变化" in result.volume_trend


def _bars():
    dates = pd.bdate_range(end="2026-10-19", periods=25)
    values = [50 + x * 0.2 for x in range(25)]
    vols = [100] * 24 + [200]
    return pd.DataFrame({"date": dates, "close": values, "open": values,
                         "high": values, "low": values, "volume": vols})


def test_us_index_list_uses_ndx_not_ixic():
    from data_provider.yfinance_fetcher import YfinanceFetcher
    import yfinance as yf
    class MockTicker:
        def history(self, **_kw):
            return pd.DataFrame({"Open": [100, 101], "High": [101, 102],
                                 "Low": [99, 100], "Close": [100, 101],
                                 "Volume": [10, 11]},
                                index=pd.DatetimeIndex(["2026-10-16", "2026-10-19"]))
    with patch.object(yf, "Ticker", return_value=MockTicker()):
        rows = YfinanceFetcher()._get_us_main_indices(yf)
    symbols = {x["code"] for x in rows}
    assert "NDX" in symbols
    assert "IXIC" not in symbols



def test_unverified_institutional_flow_is_suppressed_deterministically():
    from types import SimpleNamespace
    from src.services.report_evidence_guard import redact_unverified_flow_lines
    unsupported = SimpleNamespace(fundamental_context={
        "capital_flow": {"status": "not_supported", "data": {}}
    })
    report = "日量比0.64倍\n主力资金入场护盘明显。\n资金流数据缺失，无法判断。\n"
    cleaned = redact_unverified_flow_lines(report, [unsupported])
    assert "日量比0.64倍" in cleaned
    assert "主力资金入场护盘明显" not in cleaned
    assert "相关推断已隐藏" in cleaned
    assert "资金流数据缺失，无法判断" in cleaned
    evidenced = SimpleNamespace(fundamental_context={
        "capital_flow": {"status": "ok", "data": {"latest": [{"amount": 1}]}}
    })
    assert redact_unverified_flow_lines(report, [evidenced]) == report


def test_unverified_us_income_statement_is_quarantined_before_prompt():
    from src.services.filing_evidence import quarantine_unverified_financials
    source = {"earnings": {"status": "ok", "data": {"financial_report": {
        "report_date": "2026-06-30", "revenue": 7800000, "net_profit_parent": -540000
    }, "dividend": {"ttm_event_count": 0}}},
    "capital_flow": {"status": "not_supported", "data": {}}}
    guarded = quarantine_unverified_financials(source, market="us")
    statement = guarded["earnings"]["data"]["financial_report"]
    assert "revenue" not in statement
    assert "net_profit_parent" not in statement
    assert guarded["earnings"]["data"]["dividend"]["ttm_event_count"] == 0
    assert source["earnings"]["data"]["financial_report"]["revenue"] == 7800000
    assert quarantine_unverified_financials(source, market="hk") is source

def test_gold_spot_live_provider_has_units_timestamp_and_no_etf_proxy():
    import json
    import io
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock

    dt = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    mock = MagicMock()
    mock.__enter__.return_value = io.BytesIO(json.dumps({
        "symbol": "XAU", "currency": "USD", "price": 4301.25,
        "updatedAt": dt
    }).encode("utf-8"))
    with patch("urllib.request.urlopen", return_value=mock), patch(
        "akshare.futures_main_sina", side_effect=Exception("unavailable")
    ):
        text = build_gold_benchmark_section()
    assert "4301.25" in text
    assert "Gold-API" in text
    assert "美元/金衡盎司" in text
    assert "（参考报价）" in text
    assert "ETF" in text


def test_market_review_excludes_vix_from_other_session():
    from types import SimpleNamespace
    from src.market_analyzer import MarketAnalyzer, MarketIndex

    analyzer = MarketAnalyzer.__new__(MarketAnalyzer)
    analyzer.region = "us"
    analyzer.profile = SimpleNamespace(has_market_stats=False, has_sector_rankings=False)
    fake = [
        MarketIndex(code="SPX", name="标普500", trade_date="2026-10-19"),
        MarketIndex(code="NDX", name="纳斯达克100", trade_date="2026-10-19"),
        MarketIndex(code="DJI", name="道琼斯", trade_date="2026-10-19"),
        MarketIndex(code="VIX", name="VIX", trade_date="2026-10-20"),
    ]
    with patch.object(analyzer, "_get_main_indices", return_value=fake):
        result = analyzer.get_market_overview()
    assert result.date == "2026-10-19"
    assert [x.code for x in result.indices] == ["SPX", "NDX", "DJI"]
    assert "VIX" in analyzer._us_out_of_session_notes[0]
    assert "2026-10-20" in analyzer._us_out_of_session_notes[0]

def test_unsourced_issuer_growth_percentages_are_removed():
    from src.services.filing_evidence import quarantine_unverified_financials
    context = {
        "earnings": {"status": "ok", "data": {"financial_report": {
            "report_date": "2026-06-30", "revenue": 10000
        }}},
        "growth": {"status": "ok", "data": {
            "revenue_yoy": 91.94, "net_profit_yoy": 46.33,
            "gross_margin": 51.88
        }},
    }
    sanitized = quarantine_unverified_financials(context, market="us")
    assert sanitized["growth"]["data"] == {}
    assert sanitized["growth"]["status"] == "partial"
    assert context["growth"]["data"]["gross_margin"] == 51.88


def test_unsupported_capital_absorption_is_redacted():
    from types import SimpleNamespace
    from src.services.report_evidence_guard import redact_unverified_flow_lines
    evidence_absent = SimpleNamespace(fundamental_context={
        "capital_flow": {"status": "not_supported", "data": {}}
    })
    source = "缩量上涨，缺乏资金承接。\n"
    sanitized = redact_unverified_flow_lines(source, [evidence_absent])
    assert "缺乏资金承接" not in sanitized
    assert "已隐藏" in sanitized
