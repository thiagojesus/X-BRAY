from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from datafetchers import focus
from source_ingestion import (
    AnbimaIngestion,
    B3DiIngestion,
    BcbSgsIngestion,
    FetchResult,
    FocusIngestion,
    SidraIngestion,
    TreasuryIngestion,
    reset_refresh_context,
    set_refresh_context,
)


def test_source_adapter_records_success_and_reference_bounds() -> None:
    first = {"data": "2026-10-01", "value": 1}
    latest = {"data": "2026-10-03", "value": 2}
    with (
        patch("source_ingestion.start_source_extraction") as start,
        patch("source_ingestion.finish_source_extraction") as finish,
    ):
        result = FocusIngestion().fetch(
            indicator="IPCA", qualifiers={"kind": "annual"},
            operation=lambda: FetchResult(
                [first, latest], received_count=2, valid_count=2,
                persisted_count=2, request_count=1,
            ),
        )

    assert result == [first, latest]
    start.assert_called_once()
    finish.assert_called_once()
    record = finish.call_args.args[0]
    assert record["source"] == "focus"
    assert record["indicator"] == "IPCA"
    assert record["network_called"] == 1
    assert record["source_data_date"] == "2026-10-03"
    assert record["source_reference_min"] == "2026-10-01"
    assert record["source_reference_max"] == "2026-10-03"
    assert record["received_count"] == record["valid_count"] == 2
    assert record["inserted_count"] is None


def test_source_adapter_cache_hit_does_not_fabricate_extraction_time() -> None:
    records = [{"Data": "2026-10-03", "Mediana": 4.2}]
    with (
        patch("source_ingestion.start_source_extraction"),
        patch("source_ingestion.finish_source_extraction") as finish,
    ):
        FocusIngestion().fetch(
            indicator="IPCA", qualifiers={"kind": "annual"},
            operation=lambda: records, network_expected=False,
        )

    saved = finish.call_args.args[0]
    assert saved["status"] == "cache_hit"
    assert saved["network_called"] == 0
    assert saved["extracted_at"] is None


def test_source_adapter_persists_typed_failure_and_reraises() -> None:
    with (
        patch("source_ingestion.start_source_extraction"),
        patch("source_ingestion.finish_source_extraction") as finish,
        pytest.raises(TimeoutError),
    ):
        FocusIngestion().fetch(
            indicator="IPCA", qualifiers={},
            operation=lambda: (_ for _ in ()).throw(TimeoutError("offline")),
        )

    assert finish.call_args.args[0]["status"] == "network_error"
    assert finish.call_args.args[0]["error_code"] == "TimeoutError"


@pytest.mark.parametrize("adapter", [
    BcbSgsIngestion(), SidraIngestion(), B3DiIngestion(),
    AnbimaIngestion(), TreasuryIngestion(),
])
def test_provider_adapters_use_the_shared_template(adapter) -> None:
    with (
        patch("source_ingestion.start_source_extraction"),
        patch("source_ingestion.finish_source_extraction") as finish,
    ):
        assert adapter.fetch(indicator="test", qualifiers={}, operation=list) == []
    assert finish.call_args.args[0]["source"] == adapter.source
    assert finish.call_args.args[0]["status"] == "empty"


def test_extraction_carries_context_and_provider_error_status() -> None:
    token = set_refresh_context("run-1", "focus", "cycle-1")
    try:
        with (
            patch("source_ingestion.start_source_extraction") as start,
            patch("source_ingestion.finish_source_extraction") as finish,
        ):
            FocusIngestion().fetch(
                indicator="IPCA", qualifiers={},
                operation=lambda: {"error": "not copied into ledger"},
            )
        assert start.call_args.args[0]["run_id"] == "run-1"
        assert start.call_args.args[0]["cycle_id"] == "cycle-1"
        assert finish.call_args.args[0]["status"] == "network_error"
        assert finish.call_args.args[0]["error_detail"] == "not copied into ledger"
    finally:
        reset_refresh_context(token)


def test_explicit_fetch_result_keeps_counts_nullable_when_unknown() -> None:
    with (
        patch("source_ingestion.start_source_extraction"),
        patch("source_ingestion.finish_source_extraction") as finish,
    ):
        FocusIngestion().fetch(
            indicator="IPCA", qualifiers={},
            operation=lambda: FetchResult([], received_count=0, valid_count=0,
                                          inserted_count=None, updated_count=None),
        )
    row = finish.call_args.args[0]
    assert row["received_count"] == 0
    assert row["inserted_count"] is None
    assert row["updated_count"] is None


def test_treasury_result_uses_actual_committed_row_count() -> None:
    from source_ingestion import TreasuryIngestion

    with (
        patch("source_ingestion.start_source_extraction"),
        patch("source_ingestion.finish_source_extraction") as finish,
    ):
        TreasuryIngestion().fetch(
            indicator="bonds", qualifiers={}, operation=lambda: {"rows": 7},
        )
    row = finish.call_args.args[0]
    assert row["status"] == "success"
    assert row["received_count"] == row["valid_count"] == row["persisted_count"] == 7


def test_db_stats_unions_legacy_and_canonical_focus_identities(tmp_path, monkeypatch) -> None:
    from db import store

    store._conn = None
    monkeypatch.setattr(store, "DATABASE_URL", "")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "focus-stats.db")
    store.init_db()
    try:
        date = "2026-10-01"
        store._execute(
            "INSERT INTO focus_forecasts (indicator, date, identity_key, data_json) "
            "VALUES (?, ?, ?, ?)",
            ("IPCA", date, "2026|0|S", '{"Data":"2026-10-01","DataReferencia":2026,"baseCalculo":0,"Suavizada":"S"}'),
        )
        store._execute(
            "INSERT INTO focus_forecasts (indicator, date, identity_key, data_json) "
            "VALUES (?, ?, ?, ?)",
            ("IPCA", date, "2027|0|S", '{"Data":"2026-10-01","DataReferencia":2027,"baseCalculo":0,"Suavizada":"S"}'),
        )
        store._execute(
            "INSERT INTO focus (indicator, date, data_json) VALUES (?, ?, ?)",
            ("IPCA", date, '{"Data":"2026-10-01","DataReferencia":2026,"baseCalculo":0,"Suavizada":"S"}'),
        )  # Same forecast copied during migration; it must count only once.
        store._execute(
            "INSERT INTO focus (indicator, date, data_json) VALUES (?, ?, ?)",
            ("Selic", date, '{"Data":"2026-10-01","DataReferencia":2026,"baseCalculo":1,"Suavizada":""}'),
        )  # Legacy-only identity remains represented.

        assert store.db_stats()["focus"] == 3
    finally:
        store._conn.close()
        store._conn = None


def test_focus_annual_freshness_is_per_family_and_all_families_fetch() -> None:
    now = datetime.now(timezone.utc).isoformat()
    def response_for(*_args, **kwargs):
        indicator = kwargs["params"]["$filter"].split("'")[1]
        response = MagicMock()
        response.json.return_value = {"value": [{
            "Data": now[:10], "DataReferencia": 2026, "Indicador": indicator,
            "baseCalculo": 0, "Suavizada": "S", "Mediana": 4.2,
        }]}
        return response

    with (
        patch.object(focus, "get_meta", side_effect=lambda key: {
            "focus_annual_last_refresh_IPCA": now,
        }.get(key)),
        patch.object(focus, "query_focus", return_value={"IPCA": [{
            "Data": now[:10], "DataReferencia": 2026, "Indicador": "IPCA",
            "baseCalculo": 0, "Suavizada": "S", "Mediana": 4.2,
        }]}),
        patch.object(focus, "get_provider_response", side_effect=response_for) as request,
        patch.object(focus, "upsert_focus") as upsert,
        patch.object(focus, "set_meta"),
        patch("source_ingestion.start_source_extraction"),
        patch("source_ingestion.finish_source_extraction"),
    ):
        result = focus.fetch_all_focus(use_cache=True)
        assert focus._needs_refresh("IPCA") is False
        assert focus._needs_refresh("Selic") is True

    assert request.call_count == 4
    assert set(result) == set(focus.INDICATORS)
    assert upsert.call_count == 4


def test_focus_annual_request_uses_unescaped_odata_order_expression() -> None:
    response = MagicMock()
    response.json.return_value = {"value": [{
        "Data": "2026-10-01", "DataReferencia": 2026, "Indicador": "IPCA",
        "baseCalculo": 0, "Suavizada": "S", "Mediana": 4.2,
    }]}
    with (
        patch.object(focus, "get_meta", return_value=None),
        patch.object(focus, "get_provider_response", return_value=response) as request,
        patch.object(focus, "upsert_focus"),
        patch.object(focus, "set_meta"),
        patch("source_ingestion.start_source_extraction"),
        patch("source_ingestion.finish_source_extraction"),
    ):
        focus.fetch_focus("IPCA", use_cache=False)

    assert request.call_args.kwargs["params"]["$orderby"] == "Data desc"


def test_focus_invalid_annual_payload_does_not_write_or_mark_fresh() -> None:
    response = MagicMock()
    response.json.return_value = {"value": [{"Data": "2026-10-01", "Mediana": 4.2}]}
    with (
        patch.object(focus, "get_provider_response", return_value=response),
        patch.object(focus, "upsert_focus") as upsert,
        patch.object(focus, "set_meta") as set_meta,
        patch("source_ingestion.start_source_extraction"),
        patch("source_ingestion.finish_source_extraction"),
        pytest.raises(focus.FocusAnnualRefreshError),
    ):
        focus.fetch_focus("IPCA", use_cache=False)
    upsert.assert_not_called()
    set_meta.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        ("network", "network_error"),
        ("json", "validation_error"),
        ("validation", "validation_error"),
        ("persistence", "persistence_error"),
        ("freshness", "freshness_error"),
    ],
)
def test_focus_annual_failure_phase_has_typed_ledger_status(failure, expected_status) -> None:
    response = MagicMock()
    response.json.side_effect = ValueError("html response") if failure == "json" else None
    response.json.return_value = {"value": [{
        "Data": "2026-10-01", "DataReferencia": 2026, "Indicador": "IPCA",
        "baseCalculo": 0, "Suavizada": "S", "Mediana": 4.2,
    }]}
    network_error = OSError("offline") if failure == "network" else None
    persist_error = RuntimeError("write failed") if failure == "persistence" else None
    freshness_error = RuntimeError("metadata failed") if failure == "freshness" else None
    with (
        patch.object(focus, "get_meta", return_value=None),
        patch.object(
            focus, "get_provider_response",
            side_effect=network_error if network_error is not None else None,
            return_value=response,
        ),
        patch.object(focus, "upsert_focus", side_effect=persist_error),
        patch.object(focus, "set_meta", side_effect=freshness_error),
        patch("source_ingestion.start_source_extraction"),
        patch("source_ingestion.finish_source_extraction") as finish,
    ):
        if failure == "validation":
            response.json.return_value = {"value": [{"Data": "2026-10-01", "Mediana": 4.2}]}
        with pytest.raises(focus.FocusAnnualRefreshError):
            focus.fetch_focus("IPCA", use_cache=False)

    row = finish.call_args.args[0]
    assert row["status"] == expected_status
    assert row["network_called"] == 1
    assert row["error_detail"] in {
        "provider_failure", "invalid_payload", "persistence_failure", "freshness_write_failure",
    }
    if failure in {"persistence", "freshness"}:
        assert row["received_count"] == row["valid_count"] == 1
        assert row["source_data_date"] == "2026-10-01"
    if failure == "freshness":
        assert row["persisted_count"] == 1


def test_focus_batch_failures_do_not_count_as_successful_series() -> None:
    from refresh import RefreshTask, run_refresh_task_detailed

    now = datetime.now(timezone.utc).date().isoformat()
    def fetch_indicator(indicator, use_cache=True):
        if indicator == "IPCA":
            raise focus.FocusAnnualRefreshError("provider_failure", stage="network")
        return [{"Data": now, "DataReferencia": 2026, "Mediana": 4.2}]

    with (
        patch.object(focus, "fetch_focus", side_effect=fetch_indicator),
        patch("refresh.record_refresh_task_start"),
        patch("refresh.record_refresh_task_finish") as finish,
    ):
        result = run_refresh_task_detailed(RefreshTask(
            "focus", "focus", "olinda.bcb.gov.br", 5,
            lambda: focus.fetch_all_focus(use_cache=False),
        ))
    assert result.result["status"] == "partial"
    assert result.result["error_code"] == "PartialSourceFailures"
    assert result.series_updated == 4
    assert result.record_count == 4
    assert finish.call_args.args[1:4] == ("partial", 4, 4)


def test_focus_all_families_failed_yields_task_error_zero_counts() -> None:
    from refresh import RefreshTask, run_refresh_task_detailed

    def fail(indicator, use_cache=True):
        raise focus.FocusAnnualRefreshError("provider_failure", stage="network")

    with (
        patch.object(focus, "fetch_focus", side_effect=fail),
        patch("refresh.record_refresh_task_start"),
        patch("refresh.record_refresh_task_finish") as finish,
    ):
        result = run_refresh_task_detailed(RefreshTask(
            "focus", "focus", "olinda.bcb.gov.br", 5,
            lambda: focus.fetch_all_focus(use_cache=False),
        ))
    assert result.result["status"] == "error"
    assert result.series_updated == result.record_count == 0
    assert finish.call_args.args[1:4] == ("error", 0, 0)


def test_postcommit_focus_freshness_error_preserves_committed_counts() -> None:
    from refresh import RefreshTask, run_refresh_task_detailed

    def provider_response(*_args, **kwargs):
        provider_indicator = kwargs["params"]["$filter"].split("'")[1]
        response = MagicMock()
        response.json.return_value = {"value": [
            {"Data": "2026-10-01", "DataReferencia": 2026, "Indicador": provider_indicator,
             "baseCalculo": 0, "Suavizada": "S", "Mediana": 4.2},
        ]}
        return response

    def set_meta_for_indicator(key, _value):
        if key == focus._annual_meta_key("IPCA"):
            raise RuntimeError("metadata write failed")

    with (
        patch.object(focus, "get_meta", return_value=None),
        patch.object(focus, "get_provider_response", side_effect=provider_response),
        patch.object(focus, "upsert_focus"),
        patch.object(focus, "set_meta", side_effect=set_meta_for_indicator),
        patch("source_ingestion.start_source_extraction"),
        patch("source_ingestion.finish_source_extraction") as ledger_finish,
        patch("refresh.record_refresh_task_start"),
        patch("refresh.record_refresh_task_finish") as task_finish,
    ):
        result = run_refresh_task_detailed(RefreshTask(
            "focus", "focus", "olinda.bcb.gov.br", 5,
            lambda: focus.fetch_all_focus(use_cache=False),
        ))
    assert result.result["status"] == "partial"
    assert result.series_updated == 5
    assert result.record_count == 5
    assert task_finish.call_args.args[1:4] == ("partial", 5, 5)
    ipca_attempt = next(
        call.args[0] for call in ledger_finish.call_args_list
        if call.args[0]["indicator"] == "IPCA"
    )
    assert ipca_attempt["status"] == "freshness_error"
    assert ipca_attempt["persisted_count"] == 1
    assert ipca_attempt["source_data_date"] == "2026-10-01"


def test_refresh_counts_surface_individual_provider_failures_as_gaps() -> None:
    from refresh import _payload_counts

    assert _payload_counts({"IPCA": [{"Data": "2026-10-16"}], "Selic": {"error": "failed"}}) == (1, 1, True)
