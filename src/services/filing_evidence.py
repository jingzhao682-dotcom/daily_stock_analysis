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
