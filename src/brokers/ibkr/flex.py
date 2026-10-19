"""Validate an Activity Flex XML snapshot without creating synthetic ledger events.

Only SUMMARY OpenPosition rows are accepted. Values retain the statement's
currency and FX basis. Cash + reported positions is explicitly not broker NAV.
No credentials, account identifiers or free-form report text enter the result.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, BinaryIO
from xml.etree import ElementTree as ET

MAX_REPORT_BYTES = 5 * 1024 * 1024
MAX_REPORT_AGE_DAYS = 4
_US_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "ISLAND", "EDGEA", "EDGX", "IEX", "MEMX", "NYSEARCA", "NYSEAMERICAN"}


class IBKRFlexError(ValueError):
    """An incomplete or ambiguous report cannot be used as a holding snapshot."""


def read_report(stream: BinaryIO) -> bytes:
    content = stream.read(MAX_REPORT_BYTES + 1)
    if len(content) > MAX_REPORT_BYTES:
        raise IBKRFlexError("Flex report exceeds the 5 MiB limit")
    return content


def load_report(path: str, *, account: str | None = None) -> dict[str, Any]:
    if not path:
        raise IBKRFlexError("--ibkr-flex-file is required for --portfolio ibkr")
    try:
        with Path(path).open("rb") as stream:
            return parse_report(read_report(stream), account=account)
    except OSError:
        raise IBKRFlexError("Cannot read the selected local Flex report file") from None


def _number(row: ET.Element, key: str) -> Decimal:
    try:
        value = Decimal(row.attrib[key])
    except (KeyError, InvalidOperation, ValueError):
        raise IBKRFlexError(f"Missing or invalid Flex field: {key}") from None
    if not value.is_finite() or abs(value) > Decimal("1e15"):
        raise IBKRFlexError(f"Non-finite or out-of-range Flex field: {key}")
    return value


def _date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date() if len(value) == 8 else date.fromisoformat(value)
    except (ValueError, TypeError):
        raise IBKRFlexError("Missing or invalid Flex report date") from None


def _currency(value: str) -> str:
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise IBKRFlexError("Missing or invalid Flex currency")
    return value


def parse_report(content: bytes, *, account: str | None = None, today: date | None = None) -> dict[str, Any]:
    """Return one account snapshot from Activity Flex XML or multi-section CSV."""
    if not content or len(content) > MAX_REPORT_BYTES:
        raise IBKRFlexError("Empty or oversized Flex report")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise IBKRFlexError("Flex report must be UTF-8") from None
    if text.lstrip().startswith('"ClientAccountID"') or text.lstrip().startswith("ClientAccountID"):
        return _parse_csv_report(content, text, account=account, today=today)
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper() or "\x00" in text:
        raise IBKRFlexError("XML entities and document types are not supported")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise IBKRFlexError("Invalid Flex XML") from None
    if root.tag != "FlexQueryResponse":
        raise IBKRFlexError("Expected an Activity FlexQueryResponse XML report")
    statements = root.findall("./FlexStatements/FlexStatement")
    if account:
        statements = [row for row in statements if row.get("accountId") == account.strip()]
    if len(statements) != 1:
        raise IBKRFlexError("Select exactly one account and one report period; use --ibkr-account for a multi-account file")
    statement = statements[0]
    account_id = statement.get("accountId")
    if not account_id:
        raise IBKRFlexError("Missing statement account identity")
    report_date = _date(statement.get("toDate", ""))
    reference_date = today or datetime.now(timezone.utc).date()
    if report_date > reference_date:
        raise IBKRFlexError("Flex report date is in the future")
    age_days = (reference_date - report_date).days
    info = statement.find("AccountInformation")
    if info is None:
        raise IBKRFlexError("Include Account Information and its Currency field in the Flex query")
    if info.get("accountId", account_id) != account_id:
        raise IBKRFlexError("Account Information does not match its statement")
    currency = _currency(info.get("currency", ""))
    rows = statement.find("OpenPositions")
    if rows is None:
        raise IBKRFlexError("Include Open Positions, SUMMARY level, even for an empty account")
    positions = []
    identities: set[str] = set()
    for row in rows:
        if row.tag != "OpenPosition" or row.get("levelOfDetail") != "SUMMARY":
            raise IBKRFlexError("Open Positions must contain SUMMARY rows only; exclude lots and subtotals")
        if row.get("reportDate") and _date(row.get("reportDate")) != report_date:
            raise IBKRFlexError("Position dates do not match the statement end date")
        if row.get("accountId", account_id) != account_id:
            raise IBKRFlexError("Position account does not match its statement")
        symbol = row.get("symbol", "")
        # Restrict free-form provider text before rendering it into prompts or reports.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._/\-]{0,63}", symbol):
            raise IBKRFlexError("Invalid or unsupported position symbol")
        conid = row.get("conid", "")
        if not conid.isdigit() or conid in identities:
            raise IBKRFlexError("Missing or duplicate position contract identifier")
        identities.add(conid)
        asset_category = row.get("assetCategory", "")
        if not re.fullmatch(r"[A-Z]{2,8}", asset_category):
            raise IBKRFlexError("Missing or invalid asset category")
        position_currency = _currency(row.get("currency", ""))
        fx = _number(row, "fxRateToBase")
        quantity = _number(row, "position")
        price = _number(row, "markPrice")
        value = _number(row, "positionValue")
        cost = _number(row, "costBasisMoney")
        multiplier = _number(row, "multiplier")
        if fx <= 0 or price < 0 or multiplier <= 0 or (position_currency == currency and fx != 1):
            raise IBKRFlexError("Invalid price, multiplier or FX basis")
        if abs(quantity * price * multiplier - value) > max(Decimal("0.02"), abs(value) * Decimal("0.0001")):
            raise IBKRFlexError("Position quantity, mark price and market value disagree")
        if quantity == 0:
            if value != 0 or cost != 0:
                raise IBKRFlexError("Zero quantity has nonzero market value or cost")
            continue
        if quantity * cost < 0:
            raise IBKRFlexError("Position quantity and cost basis disagree in direction")
        supported = (asset_category == "STK" and position_currency == "USD" and multiplier == 1
                     and row.get("listingExchange", "").upper() in _US_EXCHANGES
                     and bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", symbol)))
        positions.append({
            "symbol": symbol, "asset_category": asset_category, "currency": position_currency,
            "quantity": float(quantity), "avg_cost": float(cost / quantity / multiplier),
            "mark_price": float(price), "market_value_base": float(value * fx),
            "cost_basis_base": float(cost * fx), "fx_rate_to_base": float(fx),
            "unrealized_pnl_base": float((value - cost) * fx),
            "analysis_supported": supported,
        })
    supported_symbols = [row["symbol"] for row in positions if row["analysis_supported"]]
    if len(set(supported_symbols)) != len(supported_symbols):
        raise IBKRFlexError("Ambiguous supported symbols across multiple contracts")
    cash_rows = statement.findall("./CashReport/CashReportCurrency")
    summary = [row for row in cash_rows if row.get("currency") == "BASE_SUMMARY"]
    if len(summary) == 1:
        cash_row = summary[0]
    elif not summary and len(cash_rows) == 1 and cash_rows[0].get("currency") == currency:
        cash_row = cash_rows[0]
    else:
        raise IBKRFlexError("Include one Cash Report base-currency summary; do not sum summary and currency details")
    if cash_row.get("accountId", account_id) != account_id:
        raise IBKRFlexError("Cash account does not match its statement")
    if cash_row.get("toDate") and _date(cash_row.get("toDate")) != report_date:
        raise IBKRFlexError("Cash date does not match the statement end date")
    cash = float(_number(cash_row, "endingCash"))
    market_value = sum(row["market_value_base"] for row in positions)
    assets = market_value + cash
    gross = sum(abs(row["market_value_base"]) for row in positions)
    for row in positions:
        row["weight_of_reported_assets_pct"] = row["market_value_base"] / assets * 100 if assets > 0 else None
    limitations = ["statement_snapshot_not_live", "cash_plus_positions_not_broker_nav", "cost_fx_at_statement_rate_not_total_return",
                   "no_etf_lookthrough_or_historical_drawdown", "no_target_allocation_configured"]
    if age_days > MAX_REPORT_AGE_DAYS:
        limitations.append("stale_statement")
    if any(not row["analysis_supported"] for row in positions):
        limitations.append("some_positions_not_supported_for_stock_analysis")
    return {
        "source": "ibkr_flex", "as_of": report_date.isoformat(), "currency": currency,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "report_fingerprint": hashlib.sha256(content).hexdigest(),
        "age_days": age_days, "stale": age_days > MAX_REPORT_AGE_DAYS,
        "data_quality": "partial", "limitations": limitations, "read_only": True,
        "total_cash": cash, "total_market_value": market_value,
        "reported_assets_total": assets, "gross_exposure": gross,
        "positions": positions,
    }


def _csv_number(row: dict[str, str], key: str) -> Decimal:
    try:
        value = Decimal(row[key])
    except (KeyError, InvalidOperation, ValueError):
        raise IBKRFlexError(f"Missing or invalid Flex CSV field: {key}") from None
    if not value.is_finite() or abs(value) > Decimal("1e15"):
        raise IBKRFlexError(f"Non-finite or out-of-range Flex CSV field: {key}")
    return value


def _parse_csv_report(content: bytes, text: str, *, account: str | None, today: date | None) -> dict[str, Any]:
    """Parse IBKR's repeated-header CSV without retaining account identifiers."""
    raw_rows = list(csv.reader(io.StringIO(text)))
    if not raw_rows or any(len(row) > 128 for row in raw_rows):
        raise IBKRFlexError("Invalid Flex CSV")
    sections: dict[tuple[str, ...], list[dict[str, str]]] = {}
    header: tuple[str, ...] | None = None
    for values in raw_rows:
        if not values or not any(value.strip() for value in values):
            continue
        values = [value.strip() for value in values]
        if values[0] == "ClientAccountID":
            header = tuple(values)
            if len(header) != len(set(header)):
                raise IBKRFlexError("Duplicate Flex CSV columns")
            sections.setdefault(header, [])
            continue
        if header is None or len(values) != len(header):
            raise IBKRFlexError("Flex CSV row does not match its section header")
        sections[header].append(dict(zip(header, values)))

    position_header = next((h for h in sections if {"Symbol", "Quantity", "CostBasisMoney", "PositionValue", "FifoPnlUnrealized", "CurrencyPrimary", "ReportDate"} <= set(h)), None)
    cash_header = next((h for h in sections if {"EndingCash", "Dividends", "Commissions", "Deposit/Withdrawals", "FromDate", "ToDate"} <= set(h)), None)
    if position_header is None or cash_header is None or len(sections[cash_header]) != 1:
        raise IBKRFlexError("Flex CSV must include one positions section and one cash/activity summary")
    all_rows = [row for rows in sections.values() for row in rows]
    accounts = {row.get("ClientAccountID", "") for row in all_rows if row.get("ClientAccountID")}
    if account:
        if account.strip() not in accounts:
            raise IBKRFlexError("Selected account is not present in the Flex CSV")
        selected = account.strip()
    elif len(accounts) == 1:
        selected = next(iter(accounts))
    else:
        raise IBKRFlexError("Select exactly one account; use --ibkr-account for a multi-account CSV")
    if any(row.get("ClientAccountID") not in {"", selected} for row in all_rows):
        raise IBKRFlexError("Flex CSV contains rows from multiple accounts")

    cash_row = sections[cash_header][0]
    report_date = _date(cash_row.get("ToDate", ""))
    if _date(cash_row.get("FromDate", "")) > report_date:
        raise IBKRFlexError("Flex CSV activity period is invalid")
    reference_date = today or datetime.now(timezone.utc).date()
    if report_date > reference_date:
        raise IBKRFlexError("Flex report date is in the future")
    position_rows = sections[position_header]
    currencies = {_currency(row.get("CurrencyPrimary", "")) for row in position_rows}
    currency = next(iter(currencies), "USD")
    if len(currencies) > 1:
        raise IBKRFlexError("Flex CSV positions use multiple currencies without FX rates")
    positions = []
    symbols: set[str] = set()
    for row in position_rows:
        if _date(row.get("ReportDate", "")) != report_date:
            raise IBKRFlexError("Position dates do not match the activity summary")
        symbol = row.get("Symbol", "")
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", symbol) or symbol in symbols:
            raise IBKRFlexError("Missing, invalid or duplicate Flex CSV position symbol")
        symbols.add(symbol)
        quantity = _csv_number(row, "Quantity")
        cost = _csv_number(row, "CostBasisMoney")
        value = _csv_number(row, "PositionValue")
        pnl = _csv_number(row, "FifoPnlUnrealized")
        if quantity == 0 or quantity * cost < 0 or abs((value - cost) - pnl) > Decimal("0.02"):
            raise IBKRFlexError("Flex CSV position quantity, cost, value and P&L disagree")
        positions.append({
            "symbol": symbol, "asset_category": "STK", "currency": currency,
            "quantity": float(quantity), "avg_cost": float(cost / quantity),
            "mark_price": float(value / quantity), "market_value_base": float(value),
            "cost_basis_base": float(cost), "fx_rate_to_base": 1.0,
            "unrealized_pnl_base": float(pnl), "analysis_supported": currency == "USD",
        })
    market_value = sum(row["market_value_base"] for row in positions)
    cash = float(_csv_number(cash_row, "EndingCash"))
    assets = market_value + cash
    for row in positions:
        row["weight_of_reported_assets_pct"] = row["market_value_base"] / assets * 100 if assets > 0 else None
    age_days = (reference_date - report_date).days
    limitations = ["statement_snapshot_not_live", "cash_plus_positions_not_broker_nav", "cost_fx_at_statement_rate_not_total_return",
                   "csv_lacks_contract_exchange_asset_type_and_fx", "no_etf_lookthrough_or_historical_drawdown", "no_target_allocation_configured"]
    if age_days > MAX_REPORT_AGE_DAYS:
        limitations.append("stale_statement")
    trade_header = next((h for h in sections if {"TradeDate", "TradePrice", "IBCommission", "Buy/Sell"} <= set(h)), None)
    transaction_header = next((h for h in sections if {"Date/Time", "Amount", "Type"} <= set(h)), None)
    return {
        "source": "ibkr_flex", "format": "csv", "as_of": report_date.isoformat(), "currency": currency,
        "imported_at": datetime.now(timezone.utc).isoformat(), "report_fingerprint": hashlib.sha256(content).hexdigest(),
        "age_days": age_days, "stale": age_days > MAX_REPORT_AGE_DAYS, "data_quality": "partial",
        "limitations": limitations, "read_only": True, "total_cash": cash, "total_market_value": market_value,
        "reported_assets_total": assets, "gross_exposure": sum(abs(row["market_value_base"]) for row in positions),
        "positions": positions,
        "activity": {"from_date": _date(cash_row["FromDate"]).isoformat(), "to_date": report_date.isoformat(),
                     "dividends": float(_csv_number(cash_row, "Dividends")), "commissions": float(_csv_number(cash_row, "Commissions")),
                     "deposits_withdrawals": float(_csv_number(cash_row, "Deposit/Withdrawals")),
                     "trade_rows": len(sections.get(trade_header, [])), "cash_transaction_rows": len(sections.get(transaction_header, []))},
    }


def position_context(snapshot: dict[str, Any], code: str) -> dict[str, Any]:
    """Return only the matching holding, with an explicit statement valuation basis."""
    matches = [row for row in snapshot["positions"] if row["symbol"] == code and row["analysis_supported"]]
    if len(matches) != 1:
        raise IBKRFlexError("Requested stock is not a supported holding in this report")
    row = matches[0]
    return {
        **row, "source": "ibkr_flex", "market": "us", "cost_method": "broker_reported",
        "valuation_currency": snapshot["currency"], "price_date": snapshot["as_of"],
        "price_source": "ibkr_flex", "price_provider": "IBKR", "price_available": True,
        "price_stale": snapshot["stale"], "total_cost": row["avg_cost"] * row["quantity"],
        "snapshot_scope": "cash_plus_reported_positions", "data_quality": "partial",
    }


def format_position_observation(context: dict[str, Any] | None, *, language: str = "zh") -> str:
    """Deterministic, account-free observation shared by prompts and reports."""
    if not isinstance(context, dict) or context.get("source") != "ibkr_flex":
        return ""
    weight = context.get("weight_of_reported_assets_pct")
    weight_text = f"{weight:.2f}%" if weight is not None else "N/A"
    state = ("过期" if context["price_stale"] else "报表快照") if language == "zh" else (
        "stale" if context["price_stale"] else "statement snapshot")
    if language == "zh":
        return (f"IBKR 持仓观察（{context['price_date']}，{state}）：{context['symbol']} 数量 {context['quantity']:g}，"
                f"单位成本 {context['avg_cost']:.4f} {context['currency']}；占报表现金及持仓净额 {weight_text}。"
                "这不是实时仓位或完整账户 NAV；未设置目标配置，不能据此推导加减仓、止损或改变长期 ETF 配置。")
    return (f"IBKR holding observation ({context['price_date']}, {state}): {context['symbol']} quantity {context['quantity']:g}, "
            f"unit cost {context['avg_cost']:.4f} {context['currency']}; {weight_text} of reported cash plus net positions. "
            "This is not a live holding or complete account NAV. No allocation target is configured; "
            "do not infer trades, stops or changes to long-term ETF allocations from it.")
