"""Fail-closed filing provenance check for AI investment context.

This checks the issuer filing URL and reporting period format; it does not
claim the numbers have been audited against the linked document content.
"""
from urllib.parse import urlparse
from datetime import date


def audit_filing_reference(payload):
    if not isinstance(payload, dict):
        return {"verified": False, "url": None, "reason": "invalid_payload"}
    url = str(payload.get("source_url") or payload.get("filing_url") or "").strip()
    period = str(payload.get("report_date") or "").strip()
    try:
        date.fromisoformat(period[:10])
    except (TypeError, ValueError):
        return {"verified": False, "url": None, "reason": "invalid_report_date"}
    try:
        uri = urlparse(url)
        host = (uri.hostname or "").lower()
        trusted = (host == "sec.gov" or host.endswith(".sec.gov") or
                   host == "www.sec.gov")
        if uri.scheme != "https" or not trusted or not uri.path.strip("/"):
            return {"verified": False, "url": None, "reason": "missing_official_filing_url"}
    except ValueError:
        return {"verified": False, "url": None, "reason": "invalid_url"}
    return {"verified": True, "url": url, "reason": "official_source_link_present"}


def quarantine_unverified_financials(context, *, market="us"):
    """Remove unsupported issuer income-statement figures from US research.

    The original provider block may remain useful for debugging, but the LLM,
    template and investment-report pipeline must not assert unaudited numbers
    without issuer filing provenance. Does not modify the input object.
    A trusted URL is a *reference gate*, not arithmetic verification of
    the document's line items.
    """
    if market != "us" or not isinstance(context, dict):
        return context

    earnings = context.get("earnings")
    if not isinstance(earnings, dict):
        earnings = {}
    data = earnings.get("data")
    if not isinstance(data, dict):
        data = {}
    statement = data.get("financial_report")
    if not isinstance(statement, dict):
        statement = {}

    growth_block = context.get("growth")
    if not statement and not (isinstance(growth_block, dict) and growth_block.get("data")):
        return context

    result = dict(context)
    gated = dict(earnings)
    gated_data = dict(data)
    evidence = audit_filing_reference(statement)
    if not evidence["verified"]:
        gated_data["financial_report"] = {
            "report_date": statement.get("report_date"),
            "verification_status": "unverified_source",
            "verification_note": "financial line items suppressed: no SEC source link",
        }
        gated["data"] = gated_data
        gated["status"] = "partial"
        # Growth and margin percentages are derived from the same issuer
        # financial statements: hide them too when the source cannot be checked.
        growth = context.get("growth")
        if isinstance(growth, dict) and isinstance(growth.get("data"), dict):
            result["growth"] = {
                **growth, "data": {}, "status": "partial",
                "errors": list(growth.get("errors") or []) + ["unverified_filing_source"],
            }
    else:
        gated_data["financial_report"] = {
            **statement,
            "verification_status": "official_link_present_not_line_item_verified",
        }
        gated["data"] = gated_data
    result["earnings"] = gated
    result["financial_evidence"] = evidence
    return result
