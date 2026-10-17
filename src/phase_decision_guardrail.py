# -*- coding: utf-8 -*-
"""Phase-aware decision guardrails for Issue #1386 P5."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from src.analysis_context_pack_prompt import CORE_DEGRADED_STATUSES
from src.market_phase_summary import render_market_phase_summary
from src.report_language import localize_confidence_level, normalize_report_language
from src.schemas.decision_action import localize_action_label

if TYPE_CHECKING:
    from src.analyzer import AnalysisResult


INTRADAY_PHASES = {"intraday", "lunch_break", "closing_auction"}
CONSERVATIVE_ACTION_PHASES = {"premarket", "non_trading", "unknown"}
CORE_DATA_BLOCKS = {"quote", "daily_bars", "technical"}

_PHASE_CONTEXT_KEYS = (
    "phase",
    "market",
    "market_local_time",
    "session_date",
    "effective_daily_bar_date",
    "is_trading_day",
    "is_market_open_now",
    "is_partial_bar",
    "minutes_to_open",
    "minutes_to_close",
    "trigger_source",
    "analysis_intent",
    "warnings",
)

_ZH_POSTMARKET_RECAP_PATTERNS = (
    "今日收盘后",
    "收盘后复盘",
    "盘后复盘",
    "明日重点关注",
    "明天重点关注",
    "完整交易日复盘",
)

_EN_POSTMARKET_RECAP_PATTERNS = (
    "after today's close",
    "after today’s close",
    "after the close",
    "post-market recap",
    "post market recap",
    "focus tomorrow",
    "tomorrow's focus",
    "tomorrow’s focus",
)

_IMMEDIATE_ACTION_MARKERS_ZH = (
    "立即买入",
    "马上买入",
    "立即加仓",
    "马上加仓",
    "立即卖出",
    "马上卖出",
    "立即减仓",
    "马上减仓",
)
_IMMEDIATE_ACTION_MARKERS_EN = ("buy now", "sell now", "immediate buy", "immediate sell", "add now", "reduce now")
_NEGATION_PREFIXES_ZH = ("暂不", "不建议", "禁止", "不要", "无需", "避免", "不能", "不可", "不宜", "勿", "不")
_NEGATION_PREFIXES_EN = ("do not", "don't", "dont", "not", "no", "avoid", "hold off", "without")

_KO_POSTMARKET_RECAP_PATTERNS = (
    "오늘 장 마감 후",
    "장 마감 후 리뷰",
    "장 마감 후",
    "마감 후 리뷰",
    "내일 주목",
    "내일 집중",
    "완전한 거래일 리뷰",
)
_IMMEDIATE_ACTION_MARKERS_KO = ("즉시 매수", "지금 매수", "즉시 비중확대", "즉시 매도", "지금 매도", "즉시 비중축소")
_NEGATION_PREFIXES_KO = ("하지", "권하지 않", "금지", "삼가", "불필요", "피하", "불가", "않", "안")


def _recap_patterns_for(language: str) -> tuple[str, ...]:
    if language == "en":
        return _EN_POSTMARKET_RECAP_PATTERNS
    if language == "ko":
        return _KO_POSTMARKET_RECAP_PATTERNS
    return _ZH_POSTMARKET_RECAP_PATTERNS


def _immediate_markers_for(language: str) -> tuple[str, ...]:
    if language == "en":
        return _IMMEDIATE_ACTION_MARKERS_EN
    if language == "ko":
        return _IMMEDIATE_ACTION_MARKERS_KO
    return _IMMEDIATE_ACTION_MARKERS_ZH


def _negations_for(language: str) -> tuple[str, ...]:
    if language == "en":
        return _NEGATION_PREFIXES_EN
    if language == "ko":
        return _NEGATION_PREFIXES_KO
    return _NEGATION_PREFIXES_ZH


def _reason_text(language: str, *, en: str, zh: str, ko: str) -> str:
    if language == "en":
        return en
    if language == "ko":
        return ko
    return zh


def apply_phase_decision_guardrails(
    result: "AnalysisResult",
    *,
    market_phase_summary: Optional[Dict[str, Any]],
    analysis_context_pack_overview: Optional[Dict[str, Any]],
    report_language: str = "zh",
    portfolio_context: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Apply phase/data-quality guardrails to an AnalysisResult in place."""

    if result is None:
        return []

    language = normalize_report_language(report_language or getattr(result, "report_language", "zh"))
    phase_summary = _safe_phase_summary(market_phase_summary)
    overview = analysis_context_pack_overview if isinstance(analysis_context_pack_overview, Mapping) else None
    adjustments: List[str] = []

    dashboard_value = getattr(result, "dashboard", None)
    if not isinstance(dashboard_value, dict):
        dashboard_value = {}
        setattr(result, "dashboard", dashboard_value)
    dashboard = dashboard_value
    phase_decision = dashboard.get("phase_decision")
    if not isinstance(phase_decision, dict):
        phase_decision = {}
    dashboard["phase_decision"] = phase_decision

    _ensure_phase_decision_shape(phase_decision)

    if phase_summary:
        phase_decision["phase_context"] = _phase_context_from_summary(phase_summary)

    merged_limitations = _merge_limitations(
        _list_strings(phase_decision.get("data_limitations")),
        _phase_warning_limitations(phase_summary, language=language),
        _overview_limitations(overview),
    )
    phase_decision["data_limitations"] = merged_limitations

    phase = _safe_text(phase_summary.get("phase")) if phase_summary else ""
    snapshot = getattr(result, "market_snapshot", None)
    snapshot_degraded = isinstance(snapshot, Mapping) and snapshot.get("data_quality") == "partial"
    core_degraded = _has_core_degraded_block(overview) or snapshot_degraded
    initially_high_confidence = _is_high_confidence(getattr(result, "confidence_level", ""))

    if core_degraded and initially_high_confidence:
        result.confidence_level = localize_confidence_level("medium", language)
        reason = _reason_text(
            language,
            en="Core quote, daily-bar, or technical data is degraded; high confidence was capped.",
            zh="核心行情、日线或技术数据受限，已限制高置信结论。",
            ko="핵심 시세·일봉·기술 데이터가 제한되어 높은 신뢰도를 하향 조정했습니다.",
        )
        _append_reason(phase_decision, reason)
        adjustments.append("confidence_capped_core_data_degraded")

    has_non_intraday_action = (
        phase in CONSERVATIVE_ACTION_PHASES
        and _has_immediate_buy_sell_signal(result, phase_decision, language=language)
    )
    if has_non_intraday_action:
        phase_decision["immediate_action"] = _safe_wait_action(language)
        reason = _reason_text(
            language,
            en="Current market phase does not support immediate intraday buy/sell action.",
            zh="当前市场阶段不支持即时盘中买卖动作。",
            ko="현재 시장 단계에서는 즉시 장중 매수/매도 동작을 지원하지 않습니다.",
        )
        _append_reason(phase_decision, reason)
        adjustments.append("non_intraday_action_adjusted")
        if initially_high_confidence:
            result.confidence_level = localize_confidence_level("low", language)
            adjustments.append("confidence_capped_non_intraday_action")

    if phase in INTRADAY_PHASES and _contains_postmarket_recap(result, phase_decision, language=language):
        reason = _reason_text(
            language,
            en="Intraday output contained post-market recap wording; replaced with phase-safe action wording.",
            zh="盘中输出包含盘后复盘口吻，已替换为阶段安全动作表述。",
            ko="장중 출력에 장 마감 후 리뷰 표현이 있어 단계에 맞는 안전한 표현으로 교체했습니다.",
        )
        _replace_postmarket_recap_fields(result, phase_decision, language=language)
        _append_reason(phase_decision, reason)
        adjustments.append("postmarket_recap_wording_adjusted")

    observation_only = isinstance(portfolio_context, Mapping) and portfolio_context.get("source") == "ibkr_flex"
    if core_degraded or observation_only:
        reason = _reason_text(
            language,
            en="Core data is limited. Treat technical readings as observations; verify data before trading. "
               "Do not change long-term allocations solely on this report.",
            zh="核心数据受限，技术读数仅作观察，核验数据后再决策；不应仅据本报告调整长期配置。",
            ko="핵심 데이터가 제한됩니다. 기술 지표는 관찰용이며 거래 전 데이터를 확인하세요. "
               "이 보고서만으로 장기 자산배분을 변경하지 마세요.",
        )
        if observation_only:
            reason = _reason_text(
                language,
                en="IBKR statement import is observation-only. Verify current holdings and define allocation rules before making portfolio changes.",
                zh="IBKR 报表导入仅用于持仓观察；须核验当前持仓并设置配置规则后，再判断是否调整组合。",
                ko="IBKR 명세서 가져오기는 관찰 전용입니다. 포트폴리오 변경 전 현재 보유와 배분 규칙을 확인하세요.",
            )
        # Use the existing action taxonomy and persisted guardrail reason so
        # downstream score/action alignment cannot resurrect a buy/sell badge.
        result.operation_advice = localize_action_label("watch", language)
        result.decision_type = "hold"
        result.action = "watch"
        result.action_label = localize_action_label("watch", language)
        if snapshot_degraded:
            # The headline otherwise reuses an unverified provider percentage
            # beside the corrected/disclosed snapshot table.
            result.change_pct = None
        result.analysis_summary = reason
        result.buy_reason = reason
        result.risk_warning = reason
        dashboard["core_conclusion"] = {
            "signal_type": result.action_label,
            "one_sentence": reason,
            "position_advice": {"no_position": reason, "has_position": reason},
        }
        # Execution levels/checklists can retain the original strong advice;
        # do not publish them after downgrading the headline action.
        dashboard["battle_plan"] = {}
        dashboard["decision_stability"] = {"applied": True, "reason": reason}
        if not has_non_intraday_action:
            phase_decision["immediate_action"] = reason
        _append_reason(phase_decision, reason)
        adjustments.append("action_downgraded_portfolio_observation" if observation_only else "action_downgraded_core_data_degraded")

    if (
        getattr(result, "news_result_count_known", False)
        and not getattr(result, "news_evidence_present", False)
    ):
        no_news = _reason_text(
            language,
            en="No news evidence available; news catalysts and sentiment are unverified.",
            zh="暂无新闻证据，新闻催化与舆情判断未经核验。",
            ko="뉴스 근거가 없어 뉴스 촉매와 심리 판단은 확인되지 않았습니다.",
        )
        for key in ("news_summary", "market_sentiment", "hot_topics"):
            if getattr(result, key, None):
                setattr(result, key, no_news)
        intelligence = dashboard.get("intelligence")
        if isinstance(intelligence, dict):
            for key in ("latest_news", "sentiment_summary"):
                if intelligence.get(key):
                    intelligence[key] = no_news
            if intelligence.get("positive_catalysts"):
                intelligence["positive_catalysts"] = []
        attribution = dashboard.get("signal_attribution")
        if isinstance(attribution, dict) and attribution.get("news_sentiment") not in (None, 0):
            # Contribution weights cannot be inferred again without evidence.
            # Remove the attribution instead of fabricating replacement weights.
            dashboard.pop("signal_attribution", None)
            adjustments.append("unsupported_news_attribution_removed")

    if adjustments:
        phase_decision["data_limitations"] = _merge_limitations(
            phase_decision.get("data_limitations"),
            [_adjustment_limitation_text(item, language=language) for item in adjustments],
        )

    return adjustments


def _ensure_phase_decision_shape(phase_decision: Dict[str, Any]) -> None:
    phase_decision.setdefault("phase_context", None)
    phase_decision.setdefault("action_window", None)
    phase_decision.setdefault("immediate_action", None)
    phase_decision["watch_conditions"] = _list_strings(phase_decision.get("watch_conditions"))
    phase_decision.setdefault("next_check_time", None)
    phase_decision.setdefault("confidence_reason", None)
    phase_decision["data_limitations"] = _list_strings(phase_decision.get("data_limitations"))


def _safe_phase_summary(value: Any) -> Optional[Dict[str, Any]]:
    summary = render_market_phase_summary(value)
    if not summary:
        return None
    return summary


def _phase_context_from_summary(summary: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: summary.get(key) for key in _PHASE_CONTEXT_KEYS if key in summary}


def _has_core_degraded_block(overview: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(overview, Mapping):
        return False
    blocks = overview.get("blocks")
    if not isinstance(blocks, list):
        return False
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        key = _safe_text(block.get("key"))
        status = _safe_text(block.get("status"))
        if key in CORE_DATA_BLOCKS and status in CORE_DEGRADED_STATUSES:
            return True
    return False


def _overview_limitations(overview: Optional[Mapping[str, Any]]) -> List[str]:
    if not isinstance(overview, Mapping):
        return []
    data_quality = overview.get("data_quality")
    if not isinstance(data_quality, Mapping):
        return []
    return _list_strings(data_quality.get("limitations"))


def _phase_warning_limitations(summary: Optional[Mapping[str, Any]], *, language: str) -> List[str]:
    if not isinstance(summary, Mapping):
        return []
    warnings = _list_strings(summary.get("warnings"))
    if not warnings:
        return []
    if language == "en":
        return [f"market phase warning: {item}" for item in warnings]
    if language == "ko":
        return [f"시장 단계 경고: {item}" for item in warnings]
    return [f"市场阶段提醒：{item}" for item in warnings]


def _merge_limitations(*groups: Any, limit: int = 5) -> List[str]:
    merged: List[str] = []
    for group in groups:
        for item in _list_strings(group):
            if item not in merged:
                merged.append(item)
            if len(merged) >= limit:
                return merged
    return merged


def _is_high_confidence(value: Any) -> bool:
    text = _safe_text(value).lower()
    return text in {"高", "high", "높음"}


def _has_immediate_buy_sell_signal(
    result: "AnalysisResult",
    phase_decision: Mapping[str, Any],
    *,
    language: str,
) -> bool:
    haystack = " ".join(
        _safe_text(value)
        for value in (
            getattr(result, "operation_advice", ""),
            phase_decision.get("immediate_action"),
        )
    ).lower()
    immediate_markers = _immediate_markers_for(language)
    if _contains_non_negated_marker(haystack, immediate_markers, language=language):
        return True
    return _safe_text(getattr(result, "decision_type", "")).lower() in {"buy", "sell"}


def _contains_non_negated_marker(text: str, markers: tuple[str, ...], *, language: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    for marker in markers:
        marker_text = marker.lower()
        start = 0
        while True:
            index = lowered.find(marker_text, start)
            if index < 0:
                break
            if not _is_negated_marker(lowered, index, language=language):
                return True
            start = index + len(marker_text)
    return False


def _is_negated_marker(text: str, marker_index: int, *, language: str) -> bool:
    window = 24 if language == "en" else 8
    prefix = text[max(0, marker_index - window):marker_index].rstrip()
    negations = _negations_for(language)
    return any(prefix.endswith(item) for item in negations)


def _contains_postmarket_recap(result: "AnalysisResult", phase_decision: Mapping[str, Any], *, language: str) -> bool:
    dashboard_value = getattr(result, "dashboard", None)
    dashboard = dashboard_value if isinstance(dashboard_value, dict) else {}
    core = dashboard.get("core_conclusion")
    core = core if isinstance(core, Mapping) else {}
    values = (
        core.get("one_sentence"),
        getattr(result, "operation_advice", ""),
        getattr(result, "analysis_summary", ""),
        phase_decision.get("immediate_action"),
    )
    patterns = _recap_patterns_for(language)
    return any(_contains_any(value, patterns) for value in values)


def _replace_postmarket_recap_fields(
    result: "AnalysisResult",
    phase_decision: Dict[str, Any],
    *,
    language: str,
) -> None:
    dashboard_value = getattr(result, "dashboard", None)
    dashboard = dashboard_value if isinstance(dashboard_value, dict) else {}
    core = dashboard.get("core_conclusion")
    if not isinstance(core, dict):
        core = {}
        dashboard["core_conclusion"] = core
    safe_action = _safe_wait_action(language)
    safe_summary = _reason_text(
        language,
        en=(
            "This is an intraday phase; use live state, watch conditions, and the next "
            "check point rather than post-market recap wording."
        ),
        zh="当前处于盘中阶段，应以实时状态、观察条件和下一次检查点为准，避免盘后复盘口径。",
        ko="현재 장중 단계이므로 장 마감 후 리뷰 표현 대신 실시간 상태·관찰 조건·다음 점검 시점을 기준으로 합니다.",
    )
    if _contains_any(core.get("one_sentence"), _patterns(language)):
        core["one_sentence"] = safe_action
    if _contains_any(getattr(result, "operation_advice", ""), _patterns(language)):
        result.operation_advice = safe_action
    if _contains_any(getattr(result, "analysis_summary", ""), _patterns(language)):
        result.analysis_summary = safe_summary
    if _contains_any(phase_decision.get("immediate_action"), _patterns(language)):
        phase_decision["immediate_action"] = safe_action


def _append_reason(phase_decision: Dict[str, Any], reason: str) -> None:
    existing = _safe_text(phase_decision.get("confidence_reason"))
    if not existing:
        phase_decision["confidence_reason"] = reason
        return
    if reason not in existing:
        phase_decision["confidence_reason"] = f"{existing}；{reason}"


def _adjustment_limitation_text(adjustment: str, *, language: str) -> str:
    if adjustment == "postmarket_recap_wording_adjusted":
        return _reason_text(
            language,
            en="post-market recap wording adjusted",
            zh="已修正盘后复盘口吻",
            ko="장 마감 후 리뷰 표현을 수정함",
        )
    if adjustment == "non_intraday_action_adjusted":
        return _reason_text(
            language,
            en="non-intraday immediate action adjusted",
            zh="非盘中阶段已修正即时买卖动作",
            ko="비장중 단계의 즉시 매매 동작을 수정함",
        )
    if adjustment == "confidence_capped_non_intraday_action":
        return _reason_text(
            language,
            en="confidence capped for non-intraday action",
            zh="非盘中阶段已限制买卖置信度",
            ko="비장중 단계 매매에 대해 신뢰도를 제한함",
        )
    if adjustment == "confidence_capped_core_data_degraded":
        return _reason_text(
            language,
            en="confidence capped due to degraded core data",
            zh="核心数据受限已降低置信度",
            ko="핵심 데이터 제한으로 신뢰도를 낮춤",
        )
    if adjustment == "action_downgraded_portfolio_observation":
        return _reason_text(
            language,
            en="IBKR statement is for holding observations only",
            zh="IBKR 报表仅用于持仓观察",
            ko="IBKR 명세서는 보유 관찰 전용",
        )
    return adjustment


def _safe_wait_action(language: str) -> str:
    return _reason_text(
        language,
        en="Wait for intraday confirmation; do not chase.",
        zh="等待盘中确认，禁止追高。",
        ko="장중 확인을 기다리고 추격 매수하지 마세요.",
    )


def _patterns(language: str) -> tuple[str, ...]:
    return _recap_patterns_for(language)


def _contains_any(value: Any, patterns: tuple[str, ...]) -> bool:
    text = _safe_text(value).lower()
    return bool(text) and any(pattern.lower() in text for pattern in patterns)


def _list_strings(value: Any, *, limit: int = 20) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        text = _safe_text(item)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple, set)):
        return ""
    return str(value).strip()
