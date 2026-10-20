# -*- coding: utf-8 -*-
"""Small no-API-key, official-source event feed for US investment monitoring.

These are **official announcements**, not a full news search service.
Never infer a price move or company earnings from RSS headlines alone.
Network traffic is opt-in via OFFICIAL_NEWS_RSS_ENABLED.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
import re
from typing import List, Tuple
from urllib.parse import urlparse

import feedparser
import requests

from src.search_service import SearchResult


# URLs copied from the publishers' official RSS directories (not guessed).
_OFFICIAL_FEEDS = (
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_monetary.xml", "federalreserve.gov"),
    ("U.S. SEC", "https://www.sec.gov/news/pressreleases.rss", "sec.gov"),
)
_UA = "DailyStockAnalysis-OfficialFeed/1.0 (https://github.com/ZhuLinsen/daily_stock_analysis)"
_MAX_FEED_BYTES = 1_000_000
_MAX_AGE_DAYS = 7
_MAX_EVENTS = 6
_TAGS = (
    (re.compile(r"\b(?:fomc|federal funds|interest rate|monetary policy)\b", re.I),
     "monetary_policy", ("SPYM", "QQQM", "SGOV", "XAU/USD")),
    (re.compile(r"\b(?:securities|disclosure|filing|exchange.trade|etf)\b", re.I),
     "market_regulation", ()),
    (re.compile(r"\b(?:nvidia|nvda)\b", re.I),
     "issuer_announcement", ("NVDA", "SOXQ")),
    (re.compile(r"\b(?:memory chips|semiconductors|micron|dram)\b", re.I),
     "industry_reference", ("SOXQ", "DRAM")),
)


@dataclass(frozen=True)
class FeedStatus:
    provider: str
    status: str
    count: int


@dataclass(frozen=True)
class OfficialEventBatch:
    results: List[SearchResult]
    statuses: List[FeedStatus]


def _plain_text(value: str, limit: int = 240) -> str:
    text = unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _valid_official_link(value: str, host: str) -> bool:
    try:
        url = urlparse(str(value or ""))
        domain = (url.hostname or "").lower()
        return (
            url.scheme == "https"
            and (domain == host or domain.endswith("." + host))
            and bool(url.path.strip("/"))
            and not url.username
            and not url.password
        )
    except ValueError:
        return False


def _utc_publication_time(entry):
    tm = entry.get("published_parsed") or entry.get("updated_parsed")
    if tm is None:
        return None
    try:
        return datetime(*tm[:6], tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _classify(title: str) -> Tuple[str, Tuple[str, ...]]:
    for pattern, category, affected in _TAGS:
        if pattern.search(title):
            return category, affected
    return "official_announcement", ()


def collect_official_events(now=None, *, request_get=None, max_age_days=_MAX_AGE_DAYS):
    """Retrieve a bounded set of official RSS entries, fail closed on missing proof.

    Missing, invalid and future publication timestamps are excluded.
    Failed feeds are recorded distinctly from successful but empty feeds.
    """
    as_of = now or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    get = request_get or requests.get
    cutoff = as_of - timedelta(days=max_age_days)
    candidates = []
    statuses = []

    for label, feed_url, host in _OFFICIAL_FEEDS:
        accepted = 0
        try:
            response = get(
                feed_url,
                headers={"User-Agent": _UA, "Accept": "application/rss+xml, application/xml, text/xml"},
                timeout=(3, 7),
                allow_redirects=False,
            )
            response.raise_for_status()
            if 300 <= response.status_code < 400:
                raise ValueError("feed redirects are disabled")
            body = response.content
            if not body or len(body) > _MAX_FEED_BYTES:
                raise ValueError("empty or oversized XML")
            feed = feedparser.parse(body)
            if feed.bozo and not feed.entries:
                raise ValueError("invalid RSS XML")
            for entry in feed.entries[:50]:
                published = _utc_publication_time(entry)
                link = str(entry.get("link") or "").strip()
                title = _plain_text(entry.get("title"), 160)
                if not (published and cutoff <= published <= as_of + timedelta(minutes=5)):
                    continue
                if not title or not _valid_official_link(link, host):
                    continue
                category, tickers = _classify(title)
                snippet = _plain_text(entry.get("summary") or "", 220)
                reason = (
                    "潜在关联标的：" + "、".join(tickers) + "（仅主题映射，非涨跌因果结论）"
                    if tickers else "官方公告：暂不映射到单一证券"
                )
                item = SearchResult(
                    title=title,
                    snippet=snippet or "官方公告原文可从链接核验",
                    url=link,
                    source=label,
                    published_date=published.date().isoformat(),
                    relevance_category=category,
                    relevance_reasons=[reason],
                    retrieved_at=as_of.isoformat(),
                )
                candidates.append((published, item))
                accepted += 1
            statuses.append(FeedStatus(label, "ok", accepted))
        except (requests.RequestException, ValueError, AttributeError, TypeError) as exc:
            statuses.append(FeedStatus(label, "unavailable", 0))
            # Intentionally no URL, response text or sensitive request metadata in logs.
            import logging
            logging.getLogger(__name__).warning(
                "官方公告源 %s 暂不可用: %s", label, type(exc).__name__
            )

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    seen = set()
    unique = []
    for _published, item in candidates:
        key = item.url.lower()
        if key not in seen:
            unique.append(item)
            seen.add(key)
        if len(unique) == _MAX_EVENTS:
            break
    return OfficialEventBatch(unique, statuses)


def render_official_event_section(batch: OfficialEventBatch) -> str:
    lines = ["### 官方公告事件（有来源，不代表涨跌因果）"]
    statuses = ", ".join(f"{s.provider}: {s.status}" for s in batch.statuses)
    lines.append(f"数据覆盖：{statuses or '未执行'}。仅包含最近7天已注明发布日期的官方公告。")
    if not batch.results:
        lines.append("本期未获取到可核验的近期官方公告；不据此推断没有重大新闻。")
        return "\n".join(lines)
    for event in batch.results:
        category = event.relevance_category or "official_announcement"
        # Escape markdown characters while preserving a validated official URL.
        title = event.title.replace("[", "［").replace("]", "］")
        lines.append(
            f"- {event.published_date} [{title}]({event.url}) "
            f"— {event.source} · {category}。"
        )
        for reason in (event.relevance_reasons or []):
            lines.append(f"  - {reason}")
    lines.append("备注：公告是已发布事件；可能影响哪些标的属于主题归类，不是上涨/下跌原因的已证实解释。")
    return "\n".join(lines)
