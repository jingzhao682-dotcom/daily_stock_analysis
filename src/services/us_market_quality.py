# -*- coding: utf-8 -*-
"""Deterministic US daily-index quote checks.

A passed arithmetic check does not imply independent price verification.
An independent_close may be supplied later from a separate provider, but
must never be invented when only one upstream source is available.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
from typing import Optional


@dataclass(frozen=True)
class QuoteAudit:
    status: str
    reasons: tuple[str, ...]
    trade_date: Optional[str]
    independent_checked: bool = False

    @property
    def trading_action_allowed(self) -> bool:
        return False  # The market-index feed never authorizes a trade.


def _positive(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def check_index_daily_quote(
    *,
    close,
    prev_close,
    change_pct,
    open_price,
    high,
    low,
    trade_date,
    as_of: date,
    independent_close=None,
    independent_date=None,
    max_age_days=5,
):
    issues = []
    price = _positive(close)
    previous = _positive(prev_close)
    if price is None or previous is None:
        issues.append("收盘价或昨收缺失/无效")

    try:
        bar_date = date.fromisoformat(str(trade_date))
    except (ValueError, TypeError):
        bar_date = None
        issues.append("行情交易日期缺失或无效")

    if bar_date is not None:
        delta = (as_of - bar_date).days
        if delta < 0:
            issues.append("行情日期晚于报告当地日期")
        elif delta > max_age_days:
            issues.append("行情过期")

    o, h, l = _positive(open_price), _positive(high), _positive(low)
    if h is not None and l is not None and h < l:
        issues.append("最高价低于最低价")
    if price is not None and h is not None and price > h * 1.000001:
        issues.append("收盘价超过最高价")
    if price is not None and l is not None and price < l * 0.999999:
        issues.append("收盘价低于最低价")
    if o is not None and h is not None and o > h * 1.000001:
        issues.append("开盘价超过最高价")
    if o is not None and l is not None and o < l * 0.999999:
        issues.append("开盘价低于最低价")

    if price is not None and previous is not None:
        expected = (price / previous - 1) * 100
        try:
            actual = float(change_pct)
        except (TypeError, ValueError):
            actual = float("nan")
        if not math.isfinite(actual) or abs(actual - expected) > 0.11:
            issues.append("报告涨跌幅与收盘/昨收不一致")

    independently_checked = False
    if independent_close is not None:
        second = _positive(independent_close)
        if second is None or independent_date != trade_date:
            issues.append("独立来源的交易日期或价格不可比较")
        elif price is not None:
            independently_checked = True
            if abs(second / price - 1) > 0.01:
                issues.append("独立来源收盘价偏差超过1%")

    if issues:
        return QuoteAudit("blocked", tuple(issues), str(trade_date or "") or None, independently_checked)
    status = "independently_checked" if independently_checked else "usable_single_source"
    reason = ("独立来源同日价差校验通过",) if independently_checked else ("仅通过内部一致性校验，尚无独立数据源复核",)
    return QuoteAudit(status, reason, str(trade_date), independently_checked)


def audit_us_overview(indices, *, as_of=None):
    """Check all expected indices; missing symbols never count as success."""
    from zoneinfo import ZoneInfo

    report_day = as_of or datetime.now(ZoneInfo("America/New_York")).date()
    expected = ("SPX", "NDX", "DJI", "VIX")
    indexed = {str(x.code).upper().lstrip("^"): x for x in indices}
    rows = []
    for code in expected:
        item = indexed.get(code)
        if item is None:
            rows.append((code, QuoteAudit("blocked", ("预期指数缺失",), None)))
        else:
            rows.append((code, check_index_daily_quote(
                close=item.current,
                prev_close=item.prev_close,
                change_pct=item.change_pct,
                open_price=item.open,
                high=item.high,
                low=item.low,
                trade_date=item.trade_date,
                as_of=report_day,
            )))
    return rows


def render_us_market_quality_section(indices, *, as_of=None):
    rows = audit_us_overview(indices, as_of=as_of)
    lines = [
        "### 行情数据质量审查（不授权交易）",
        "| 指数 | 校验状态 | 交易日期 | 核验说明 |",
        "|------|----------|----------|----------|",
    ]
    for symbol, audit in rows:
        label = "阻断" if audit.status == "blocked" else (
            "独立核验" if audit.independent_checked else "单源自检通过"
        )
        lines.append(
            f"| {symbol} | {label} | {audit.trade_date or 'N/A'} | "
            + "；".join(audit.reasons) + " |"
        )
    if any(item.status == "blocked" for _, item in rows):
        lines.append("**警告：指数存在缺失、过期或数值冲突；相关大盘判断只作观察，不得生成交易点位。**")
    else:
        lines.append("说明：单源自检不代表已完成跨供应商复核，也不是独立买卖依据。")
    return "\n".join(lines)
