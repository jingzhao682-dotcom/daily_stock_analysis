"""Offline IBKR statement contract, privacy, API and analysis-entry regressions."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

import pytest

from src.brokers.ibkr.flex import (
    IBKRFlexError, MAX_REPORT_BYTES, format_position_observation, load_report,
    parse_report, position_context, read_report,
)

TODAY = datetime.now(timezone.utc).date()
REPORT_DAY = TODAY - timedelta(days=1)


def report(**attrs):
    day = REPORT_DAY.strftime("%Y%m%d")
    values = dict(accountId="U_TEST_PRIVATE", conid="12345", symbol="QQQM", assetCategory="STK",
                  listingExchange="NASDAQ", currency="USD", fxRateToBase="1", position="10",
                  markPrice="200", positionValue="2000", costBasisMoney="1800", multiplier="1",
                  levelOfDetail="SUMMARY", reportDate=day)
    values.update(attrs)
    root = ET.Element("FlexQueryResponse")
    statements = ET.SubElement(root, "FlexStatements")
    statement = ET.SubElement(statements, "FlexStatement", accountId="U_TEST_PRIVATE", toDate=day)
    ET.SubElement(statement, "AccountInformation", currency="USD", accountId="U_TEST_PRIVATE")
    ET.SubElement(ET.SubElement(statement, "OpenPositions"), "OpenPosition", **values)
    ET.SubElement(ET.SubElement(statement, "CashReport"), "CashReportCurrency", currency="BASE_SUMMARY",
                  endingCash="8000", toDate=day)
    return root


def xml(root):
    return ET.tostring(root)


def parse(root, **kw):
    return parse_report(xml(root), today=TODAY, **kw)


def test_snapshot_has_one_consistent_currency_basis_without_account_identity():
    value = parse(report())
    assert value["reported_assets_total"] == 10000
    assert value["total_cash"] == 8000
    assert value["positions"][0]["weight_of_reported_assets_pct"] == 20
    assert value["positions"][0]["unrealized_pnl_base"] == 200
    assert value["as_of"] == REPORT_DAY.isoformat()
    assert value["read_only"] is True and value["data_quality"] == "partial"
    assert "U_TEST_PRIVATE" not in str(value)
    assert "cash_plus_positions_not_broker_nav" in value["limitations"]
    assert parse(report())["report_fingerprint"] == value["report_fingerprint"]


def test_fx_and_summary_cash_are_not_added_twice():
    root = report(currency="EUR", fxRateToBase="1.2", listingExchange="IBIS")
    cash = root.find("./FlexStatements/FlexStatement/CashReport")
    ET.SubElement(cash, "CashReportCurrency", currency="EUR", endingCash="9999")
    value = parse(root)
    assert value["total_cash"] == 8000
    assert value["total_market_value"] == 2400
    assert value["positions"][0]["analysis_supported"] is False
    assert "some_positions_not_supported_for_stock_analysis" in value["limitations"]


@pytest.mark.parametrize("key,value", [
    ("markPrice", "NaN"), ("fxRateToBase", "Infinity"), ("fxRateToBase", "0"),
    ("fxRateToBase", "1.1"), ("position", "bad"), ("positionValue", "2001"),
    ("costBasisMoney", ""), ("multiplier", "0"), ("levelOfDetail", "LOT"),
    ("reportDate", "20000101"), ("accountId", "OTHER_ACCOUNT"),
    ("symbol", "<script>"), ("conid", ""),
])
def test_inconsistent_or_partial_rows_never_produce_a_snapshot(key, value):
    with pytest.raises(IBKRFlexError):
        parse(report(**{key: value}))


def test_mult_account_and_duplicate_summary_rows_fail_closed():
    root = report()
    statements = root.find("FlexStatements")
    other = deepcopy(statements[0]); other.set("accountId", "OTHER")
    statements.append(other)
    with pytest.raises(IBKRFlexError, match="exactly one"):
        parse(root)
    assert len(parse(root, account="U_TEST_PRIVATE")["positions"]) == 1
    positions = statements[0].find("OpenPositions")
    positions.append(deepcopy(positions[0]))
    with pytest.raises(IBKRFlexError, match="duplicate"):
        parse(root, account="U_TEST_PRIVATE")


@pytest.mark.parametrize("section", ["OpenPositions", "CashReport", "AccountInformation"])
def test_missing_sections_are_not_empty_accounts(section):
    root = report(); statement = root.find("./FlexStatements/FlexStatement")
    statement.remove(statement.find(section))
    with pytest.raises(IBKRFlexError):
        parse(root)


def test_explicit_empty_account_and_negative_cash_or_short_holdings():
    root = report(); positions = root.find("./FlexStatements/FlexStatement/OpenPositions")
    positions.clear()
    assert parse(root)["positions"] == []
    short = parse(report(position="-10", positionValue="-2000", costBasisMoney="-1800"))
    assert short["positions"][0]["quantity"] == -10
    assert short["positions"][0]["weight_of_reported_assets_pct"] < 0
    root.find(".//CashReportCurrency").set("endingCash", "-10")
    assert parse(root)["reported_assets_total"] == -10


def test_staleness_future_dates_and_bounded_safe_xml():
    root = report()
    value = parse_report(xml(root), today=TODAY + timedelta(days=5))
    assert value["stale"] and "stale_statement" in value["limitations"]
    with pytest.raises(IBKRFlexError, match="future"):
        parse_report(xml(root), today=REPORT_DAY - timedelta(days=1))
    for raw in (b'<!DOCTYPE x [<!ENTITY x "secret">]><FlexQueryResponse/>', b'\xff\xfe', b'bad XML', b''):
        with pytest.raises(IBKRFlexError):
            parse_report(raw)
    stream = BytesIO(b'x' * (MAX_REPORT_BYTES + 10))
    with pytest.raises(IBKRFlexError, match="limit"):
        read_report(stream)
    assert stream.tell() == MAX_REPORT_BYTES + 1


def test_only_supported_us_stocks_can_enter_analysis_and_context_is_per_symbol():
    value = parse(report())
    context = position_context(value, "QQQM")
    assert context["symbol"] == "QQQM" and context["quantity"] == 10
    assert context["price_date"] == value["as_of"]
    with pytest.raises(IBKRFlexError):
        position_context(value, "SPYM")
    assert format_position_observation(None) == ""
    for lang in ("zh", "en"):
        note = format_position_observation(context, language=lang)
        assert "QQQM" in note and "20.00%" in note and "U_TEST_PRIVATE" not in note
    unsupported = parse(report(assetCategory="OPT", multiplier="100", positionValue="200000"))
    with pytest.raises(IBKRFlexError):
        position_context(unsupported, "QQQM")


def test_cli_import_is_explicit_and_propagates_failures_without_falling_back(tmp_path):
    import main
    path = tmp_path / "report.xml"; path.write_bytes(xml(report()))
    args = SimpleNamespace(portfolio="ibkr", ibkr_flex_file=str(path), ibkr_account=None)
    assert main._resolve_portfolio_stock_codes(args) == ["QQQM"]
    assert args._ibkr_snapshot["positions"][0]["quantity"] == 10
    with patch("sys.argv", ["main.py", "--portfolio", "ibkr", "--ibkr-flex-file", str(path)]):
        assert main.parse_arguments().portfolio == "ibkr"
    with patch("sys.argv", ["main.py", "--ibkr-flex-file", str(path)]), pytest.raises(SystemExit):
        main.parse_arguments()
    with patch("sys.argv", ["main.py", "--portfolio", "ibkr"]), pytest.raises(SystemExit):
        main.parse_arguments()
    path.write_text("invalid")
    with pytest.raises(IBKRFlexError):
        main._resolve_portfolio_stock_codes(args)
    with pytest.raises(IBKRFlexError, match="Cannot read"):
        load_report(str(tmp_path / "does-not-exist"))


def test_observation_mode_cannot_resurrect_execution_or_change_long_term_allocation():
    from src.analyzer import AnalysisResult
    from src.core.pipeline import StockAnalysisPipeline
    from src.phase_decision_guardrail import apply_phase_decision_guardrails
    from src.notification import NotificationService
    context = position_context(parse(report()), "QQQM")
    result = AnalysisResult(code="QQQM", name="QQQM", sentiment_score=99, trend_prediction="bullish",
                            operation_advice="Buy now", action="buy", decision_type="buy",
                            dashboard={"battle_plan": {"sniper_points": {"buy": 123}}})
    apply_phase_decision_guardrails(result, market_phase_summary=None, analysis_context_pack_overview=None,
                                    portfolio_context=context)
    StockAnalysisPipeline._refresh_decision_action_for_final_result(result, report_type="detailed", previous_operation_advice="Buy now")
    assert result.action == "watch" and result.decision_type == "hold"
    assert result.get_sniper_points() == {}
    assert "IBKR" in result.analysis_summary
    StockAnalysisPipeline._append_portfolio_observation(result, context, "zh")
    rendered = NotificationService().generate_dashboard_report([result])
    assert "20.00%" in rendered and "IBKR" in rendered


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import create_app
    from src.config import Config
    from src.storage import DatabaseManager
    import src.auth as auth
    env = tmp_path / ".env"
    env.write_text(f"ADMIN_AUTH_ENABLED=false\nDATABASE_PATH={tmp_path / 'test.db'}\nSTOCK_LIST=QQQM\n")
    monkeypatch.setenv("ENV_FILE", str(env))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    Config.reset_instance(); DatabaseManager.reset_instance()
    auth._auth_enabled = None
    client = TestClient(create_app(static_dir=tmp_path / "empty-static"))
    yield client
    DatabaseManager.reset_instance(); Config.reset_instance()
    auth._auth_enabled = None


def test_preview_api_never_writes_ledger_starts_analysis_or_sends_mail(api_client):
    with patch("api.v1.endpoints.portfolio.get_task_queue") as queue, patch(
        "src.repositories.portfolio_repo.PortfolioRepository.add_trade"
    ) as ledger, patch("src.notification.NotificationService.send") as mail:
        response = api_client.post("/api/v1/portfolio/imports/ibkr/flex/parse",
                                   files={"file": ("holdings.xml", xml(report()), "application/xml")})
    assert response.status_code == 200, response.text
    assert response.json()["positions"][0]["weight_of_reported_assets_pct"] == 20
    assert "U_TEST_PRIVATE" not in response.text
    queue.assert_not_called(); ledger.assert_not_called(); mail.assert_not_called()
    bad = api_client.post("/api/v1/portfolio/imports/ibkr/flex/parse",
                          files={"file": ("bad.xml", b"not XML", "application/xml")})
    assert bad.status_code == 400


def test_analysis_api_enqueues_one_matching_holding_without_notification(api_client):
    task = SimpleNamespace(task_id="test-task", trace_id="test-trace", stock_code="QQQM", analysis_phase="auto")
    with patch("api.v1.endpoints.portfolio.get_task_queue") as queue:
        queue.return_value.submit_tasks_batch.return_value = ([task], [])
        response = api_client.post("/api/v1/portfolio/imports/ibkr/flex/positions/qqqm/analysis",
                                   files={"file": ("holdings.xml", xml(report()), "application/xml")})
    assert response.status_code == 202, response.text
    kwargs = queue.return_value.submit_tasks_batch.call_args.kwargs
    assert kwargs["notify"] is False
    assert kwargs["portfolio_context"]["symbol"] == "QQQM"
    assert kwargs["portfolio_context"]["quantity"] == 10
    with patch("api.v1.endpoints.portfolio.get_task_queue") as queue:
        missing = api_client.post("/api/v1/portfolio/imports/ibkr/flex/positions/SPYM/analysis",
                                  files={"file": ("holdings.xml", xml(report()), "application/xml")})
    assert missing.status_code == 400
    queue.assert_not_called()


def test_runtime_scheduler_retains_local_import_selection():
    from src.services.runtime_scheduler import RuntimeSchedulerService
    scheduler = RuntimeSchedulerService(schedule_args_overrides={
        "portfolio": "ibkr", "ibkr_flex_file": "local.xml", "ibkr_account": "selected",
    })
    args = scheduler._make_schedule_args()
    assert args.portfolio == "ibkr" and args.ibkr_flex_file == "local.xml" and args.ibkr_account == "selected"


def test_pipeline_uses_each_holding_and_includes_the_statement_in_the_model_context():
    from src.core.pipeline import StockAnalysisPipeline
    from src.services.analysis_context_builder import PipelineAnalysisArtifacts
    snapshot = parse(report())
    other = deepcopy(snapshot["positions"][0]); other.update(symbol="SPYM", quantity=25)
    snapshot["positions"].append(other)
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.portfolio_context = snapshot
    a = pipeline._portfolio_context_for_code("QQQM")
    b = pipeline._portfolio_context_for_code("SPYM")
    assert a["quantity"] == 10 and b["quantity"] == 25
    assert "positions" not in a and "SPYM" not in str(a)
    artifacts = PipelineAnalysisArtifacts(
        code="QQQM", stock_name="QQQM", market="us", phase=None, base_context={}, enhanced_context={},
        realtime_quote=None, trend_result=None, chip_data=None, fundamental_context=None, news_context=None,
        news_result_count=0, metadata={}, portfolio_context=a,
    )
    summary, overview = pipeline._build_analysis_context_pack_outputs(
        artifacts, report_language="zh", code="QQQM", query_id="test",
    )
    assert "20.00%" in summary and REPORT_DAY.isoformat() in summary
    assert "U_TEST_PRIVATE" not in summary and "SPYM" not in summary
    assert overview is not None


def test_non_usd_base_currency_keeps_native_cost_separate_from_base_valuation():
    root = report(fxRateToBase="7")
    root.find(".//AccountInformation").set("currency", "CNY")
    value = parse(root)
    context = position_context(value, "QQQM")
    assert context["currency"] == "USD" and context["valuation_currency"] == "CNY"
    assert context["total_cost"] == 1800 and context["cost_basis_base"] == 12600
    assert context["market_value_base"] == 14000
    assert context["avg_cost"] == 180
    root.find(".//CashReportCurrency").set("endingCash", "-14000")
    assert parse(root)["positions"][0]["weight_of_reported_assets_pct"] is None
