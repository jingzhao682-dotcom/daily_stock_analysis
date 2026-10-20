# -*- coding: utf-8 -*-
"""P0 regression: 2026-10-19 US closing quotes must not become 10-20 candles.

All tests use offline, deliberately explicit OHLCV fixtures. The five latest DRAM closes are historical prices for Oct 13, 14, 15, 16, 19.
Source: https://stockanalysis.com/etf/dram/history/
"""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from data_provider.yfinance_fetcher import YfinanceFetcher
from src.core.pipeline import StockAnalysisPipeline
from src.stock_analyzer import StockTrendAnalyzer


def _history():
    return pd.DataFrame(
        {
            "Open": [46.5, 48.79],
            "High": [47.0, 49.34],
            "Low": [46.1, 48.07],
            "Close": [46.45, 48.33],
            "Volume": [1234, 30302600],
        },
        index=pd.DatetimeIndex(["2026-10-16", "2026-10-19"], tz="America/New_York"),
    )


def _quote_from_history(history):
    import yfinance as yf

    ticker = MagicMock()
    ticker.history.return_value = history
    ticker.info = {}
    # Deliberately incorrect fast_info.previousClose (as in the old report).
    ticker.fast_info.previousClose = 46.27
    ticker.fast_info.lastPrice = 48.33
    with patch.object(yf, "Ticker", return_value=ticker):
        result = YfinanceFetcher().get_realtime_quote("DRAM")
    return result, ticker


def test_us_prev_close_uses_same_raw_daily_series_not_fast_info():
    quote, ticker = _quote_from_history(_history())
    assert quote is not None
    assert quote.source == RealtimeSource.YFINANCE
    assert quote.trade_date == "2026-10-19"
    assert quote.price == pytest.approx(48.33)
    assert quote.pre_close == pytest.approx(46.45)
    assert quote.change_amount == pytest.approx(1.88)
    assert quote.change_pct == pytest.approx(4.05)
    ticker.history.assert_called_once_with(period="10d", auto_adjust=False, actions=False)
    assert quote.to_dict()["trade_date"] == "2026-10-19"


def test_us_one_day_history_does_not_invent_yesterday_or_zero_return():
    quote, _ = _quote_from_history(_history().iloc[-1:])
    assert quote is not None
    assert quote.pre_close is None
    assert quote.change_pct is None
    assert "prev_close" in quote.missing_fields


def test_duplicate_provider_session_is_not_counted_as_prior_day():
    hist = _history()
    duplicate = hist.iloc[-1:].copy()
    duplicate["Close"] = 48.33
    hist = pd.concat([hist, duplicate])
    quote, _ = _quote_from_history(hist)
    assert quote.pre_close == pytest.approx(46.45)


def test_us_historical_download_disables_adjustment():
    import yfinance as yf

    raw = _history()
    with patch.object(yf, "download", return_value=raw) as download:
        returned = YfinanceFetcher()._fetch_raw_data("DRAM", "2026-10-16", "2026-10-20")
    assert returned.equals(raw)
    assert download.call_args.kwargs["auto_adjust"] is False


def _bars():
    dates = pd.bdate_range(end="2026-10-19", periods=25)
    closes = [80.0 + n for n in range(20)] + [52.84, 50.14, 49.33, 46.45, 48.33]
    return pd.DataFrame(
        {"code": ["DRAM"] * 25, "date": [x.date() for x in dates],
         "open": closes, "high": closes, "low": closes,
         "close": closes, "volume": [1000] * 25}
    )


def _pipe():
    p = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    p.config = SimpleNamespace(enable_realtime_technical_indicators=True)
    return p


def test_us_next_calendar_day_premarket_does_not_append_phantom_bar():
    df = _bars()
    original = df.copy(deep=True)
    quote = UnifiedRealtimeQuote(
        code="DRAM", source=RealtimeSource.YFINANCE,
        trade_date="2026-10-19", price=48.33, volume=30302600
    )
    ny = datetime(2026, 10, 20, 0, 52, tzinfo=ZoneInfo("America/New_York"))
    with patch("src.core.pipeline.get_market_now", return_value=ny), \
         patch("src.core.pipeline.is_market_open", return_value=True), \
         patch("src.core.pipeline.get_effective_trading_date", return_value=date(2026, 10, 19)):
        result = _pipe()._augment_historical_with_realtime(df, quote, "DRAM", market="us")
    pd.testing.assert_frame_equal(df, original)
    pd.testing.assert_frame_equal(result, original)
    assert StockTrendAnalyzer().analyze(result, "DRAM").ma5 == pytest.approx(49.418)


def test_us_same_session_intraday_augmentation_still_works():
    df = _bars()
    quote = UnifiedRealtimeQuote(
        code="DRAM", source=RealtimeSource.YFINANCE,
        trade_date="2026-10-20", price=49.0, volume=40000
    )
    ny = datetime(2026, 10, 20, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    with patch("src.core.pipeline.get_market_now", return_value=ny), \
         patch("src.core.pipeline.is_market_open", return_value=True), \
         patch("src.core.pipeline.get_effective_trading_date", return_value=date(2026, 10, 20)):
        result = _pipe()._augment_historical_with_realtime(df, quote, "DRAM", market="us")
    assert len(result) == len(df) + 1
    assert result.iloc[-1]["date"] == date(2026, 10, 20)


def test_analyzer_deduplicates_same_session_before_moving_averages():
    df = _bars()
    doubled = pd.concat([df, df.iloc[-1:]], ignore_index=True)
    assert StockTrendAnalyzer().analyze(doubled, "DRAM").ma5 == pytest.approx(
        StockTrendAnalyzer().analyze(df, "DRAM").ma5
    )


def test_moving_average_fixture_matches_independent_five_close_mean():
    bars = _bars()
    expected = sum([52.84, 50.14, 49.33, 46.45, 48.33]) / 5
    assert expected == pytest.approx(49.418)
    assert StockTrendAnalyzer().analyze(bars, "DRAM").ma5 == pytest.approx(expected)
