"""Deterministic evidence gate for unsupported capital-flow narratives.

A 5-day traded-volume ratio is not proof of institutional order flow.
Filter asserted flows only where every reported instrument lacks a verified
capital-flow payload. Safe if already-cautious text is present.
"""
import re


_UNSUPPORTED_FLOW_CLAIM = re.compile(
    r"(主力|机构|大资金|资金)(?:[^\n，。:：；]{0,16})"
    r"(流入|流出|回流|进场|入场|撤离|抄底|护盘|抢筹|建仓|出逃)"
    r"|资金承接|买盘匮乏|资金接力|主力资金介入|抄底盘|主动性买盘|买盘动能"
)
_EXPLICIT_UNCERTAINTY = re.compile(
    r"(未核验|未经核验|缺少|数据缺失|暂无|不可用|无法判断|无法推断|不推断|未证实|没有证据)"
)


def no_verified_flow_evidence(results):
    if not results:
        return False
    for result in results:
        context = getattr(result, "fundamental_context", None)
        if not isinstance(context, dict):
            continue
        flow = context.get("capital_flow")
        if not isinstance(flow, dict):
            continue
        status = str(flow.get("status") or "").lower()
        data = flow.get("data")
        if status == "ok" and data:
            return False
    return True


def redact_unverified_flow_lines(report, results):
    if not isinstance(report, str) or not no_verified_flow_evidence(results):
        return report
    lines = []
    for line in report.splitlines(keepends=True):
        if _UNSUPPORTED_FLOW_CLAIM.search(line) and not _EXPLICIT_UNCERTAINTY.search(line):
            newline = "\n" if line.endswith("\n") else ""
            lines.append("资金流向：缺少经过核验的资金流证据，相关推断已隐藏。" + newline)
        else:
            lines.append(line)
    return "".join(lines)
