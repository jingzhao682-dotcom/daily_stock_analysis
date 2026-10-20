# -*- coding: utf-8 -*-
"""Offline, deterministic P2 regression for official news and market quote QA."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from src.services.official_news_events import (
    collect_official_events,
    render_official_event_section,
)
from src.services.us_market_quality import (
    audit_us_overview,
    check_index_daily_quote,
    render_us_market_quality_section,
)


def _rss(item_date="Mon, 19 Oct 2026 13:30:00 GMT", link="https://www.federalreserve.gov/newsevents/pressreleases/monetary20261019a.htm"):
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<rss version='2.0'><channel><title>Fed press releases</title>"
        "<item><title>Federal funds interest rate policy update</title>"
        "<description>Official monetary policy announcement</description>"
        f"<pubDate>{item_date}</pubDate><link>{link}</link></item>"
        "</channel></rss>"
    ).encode()


def _response(content):
    return SimpleNamespace(content=content, status_code=200, raise_for_status=lambda: None)


def _valid_news_get(url, **kwargs):
    assert url.startswith("https://")
    assert kwargs["allow_redirects"] is False
    if "federalreserve.gov" in url:
        return _response(_rss())
    return _response(b"<?xml version='1.0'?><rss version='2.0'><channel/></rss>")


def test_official_rss_keeps_publisher_link_date_and_weak_mapping():
    now = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
    batch = collect_official_events(now, request_get=_valid_news_get)
    assert len(batch.results) == 1
    event = batch.results[0]
    assert event.url.startswith("https://www.federalreserve.gov/")
    assert event.published_date == "2026-10-19"
    assert event.relevance_category == "monetary_policy"
    assert "QQQM" in event.relevance_reasons[0]
    assert "非涨跌因果结论" in event.relevance_reasons[0]
    assert "2026-10-19" in render_official_event_section(batch)


def test_official_rss_rejects_lookalike_domain_and_future_dates():
    now = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
    def get(url, **kwargs):
        if "federalreserve.gov" in url:
            return _response(_rss(link="https://federalreserve.gov.evil.invalid/fake"))
        return _response(_rss(item_date="Wed, 21 Oct 2026 13:30:00 GMT"))
    batch = collect_official_events(now, request_get=get)
    assert not batch.results
    assert "未获取到可核验" in render_official_event_section(batch)


def test_unavailable_official_feed_does_not_claim_no_news():
    def fail(_url, **_kwargs):
        import requests
        raise requests.Timeout("feed unavailable")
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    batch = collect_official_events(now, request_get=fail)
    assert len(batch.statuses) == 2
    assert all(x.status == "unavailable" for x in batch.statuses)
    output = render_official_event_section(batch)
    assert "unavailable" in output
    assert "不据此推断没有重大新闻" in output


def _good_quote(**overrides):
    values = dict(
        close=102, prev_close=100, change_pct=2.0,
        open_price=100.5, high=103, low=100,
        trade_date="2026-10-19", as_of=date(2026, 10, 20),
    )
    values.update(overrides)
    return check_index_daily_quote(**values)


def test_market_qa_internal_consistency_is_not_independent_verification():
    result = _good_quote()
    assert result.status == "usable_single_source"
    assert not result.independent_checked
    assert not result.trading_action_allowed


def test_market_qa_blocks_mixed_prior_close_basis():
    result = _good_quote(prev_close=95)
    assert result.status == "blocked"
    assert "昨收" in " ".join(result.reasons)


def test_market_qa_blocks_broken_ohlc_and_stale_dates():
    assert _good_quote(high=101).status == "blocked"
    assert _good_quote(trade_date="2026-10-01").status == "blocked"
    assert _good_quote(trade_date="2026-10-21").status == "blocked"


def test_market_qa_comparison_requires_matching_exchange_date():
    assert _good_quote(independent_close=102.2, independent_date="2026-10-19").status == "independently_checked"
    assert _good_quote(independent_close=108, independent_date="2026-10-19").status == "blocked"
    assert _good_quote(independent_close=102, independent_date="2026-10-20").status == "blocked"


def test_market_qa_does_not_silently_drop_missing_ndx_vix():
    overview = [
        SimpleNamespace(code=symbol, current=102, prev_close=100, change_pct=2,
                        open=100, high=103, low=100, trade_date="2026-10-19")
        for symbol in ("SPX", "DJI")
    ]
    checks = dict(audit_us_overview(overview, as_of=date(2026, 10, 20)))
    assert checks["NDX"].status == "blocked"
    assert checks["VIX"].status == "blocked"
    rendered = render_us_market_quality_section(overview, as_of=date(2026, 10, 20))
    assert "预期指数缺失" in rendered
    assert "不得生成交易点位" in rendered


def test_market_qa_opt_in_report_stays_network_free():
    # A separate explicit network switch is required for RSS.
    with patch("src.services.official_news_events.requests.get") as get:
        from src.services.official_news_events import _valid_official_link
        assert _valid_official_link("https://www.sec.gov/newsroom/press-releases/test", "sec.gov")
        assert not _valid_official_link("https://www.sec.gov.evil.invalid/x", "sec.gov")
        get.assert_not_called()


def test_market_qa_blocks_next_day_vix_against_us_equity_closing_session():
    # Reproduces 2026-10-20 Beijing morning: SPX/NDX/DJI still 10/19,
    # while VIX provider advances its date to 10/20.
    indices = [
        SimpleNamespace(code=code, current=102, prev_close=100, change_pct=2,
                        open=100.5, high=103, low=100,
                        trade_date="2026-10-20" if code == "VIX" else "2026-10-19")
        for code in ("SPX", "NDX", "DJI", "VIX")
    ]
    result = dict(audit_us_overview(indices, as_of=date(2026, 10, 20)))
    assert result["SPX"].status == "usable_single_source"
    assert result["NDX"].status == "usable_single_source"
    assert result["DJI"].status == "usable_single_source"
    assert result["VIX"].status == "blocked"
    assert "交易日期不同" in " ".join(result["VIX"].reasons)
