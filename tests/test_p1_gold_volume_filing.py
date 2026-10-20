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
