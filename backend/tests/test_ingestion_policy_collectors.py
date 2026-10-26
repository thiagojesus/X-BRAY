from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from threading import local
from unittest.mock import MagicMock

import httpx
import pytest

from db import store
from ingestion_policy import CircuitOpen, RetryDeferred, current_retry_budget


@pytest.fixture
def collector_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DATABASE_URL", "")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "collectors.sqlite")
    monkeypatch.setattr(store, "_pg_local", local())
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _spy_http(monkeypatch: pytest.MonkeyPatch, response_factory):
    calls: list[tuple[str, int, int, str | None]] = []

    def get(url: str, **kwargs):
        budget = current_retry_budget()
        assert budget is not None, "real collector HTTP must run inside its source scope"
        calls.append((url, budget.requests, budget._request_limit, budget._unit_context.get()))
        return response_factory(url, kwargs)

    monkeypatch.setattr(httpx, "get", get)
    return calls


def _response(url: str, payload: object, *, content: bytes | None = None) -> httpx.Response:
    body = content if content is not None else json.dumps(payload).encode()
    return httpx.Response(200, content=body, request=httpx.Request("GET", url))


def test_sgs_public_callback_charges_real_http_within_one_window(collector_db, monkeypatch) -> None:
    from datafetchers import bcb_sgs

    calls = _spy_http(monkeypatch, lambda url, _kwargs: _response(url, [
        {"data": "01/01/2026", "valor": "1.25"},
    ]))
    result = bcb_sgs.fetch_sgs_series(
        999999, start_date="01/01/2026", end_date="02/01/2026", use_cache=False,
    )
    assert len(result) == 1
    assert len(calls) == 1
    assert calls[0][1:] == (1, 3, "bcb_sgs:999999:01/01/2026:02/01/2026")
    assert store._query("SELECT request_count, retry_count FROM source_extractions ORDER BY started_at DESC LIMIT 1")[0] == {
        "request_count": 1, "retry_count": 0,
    }


def test_focus_group_callback_shares_five_family_root_budget(collector_db, monkeypatch) -> None:
    from datafetchers import focus

    today = datetime.now(timezone.utc).date().isoformat()
    def response(url: str, kwargs) -> httpx.Response:
        indicator = kwargs["params"]["$filter"].split("'")[1]
        value = "PIB Total" if indicator == "PIB Total" else indicator
        return _response(url, {"value": [{
            "Data": today, "DataReferencia": 2027, "Indicador": value,
            "baseCalculo": 0, "Suavizada": "S", "Mediana": 4.2,
        }]})

    calls = _spy_http(monkeypatch, response)
    result = focus.fetch_all_focus(use_cache=False)
    assert set(result) == set(focus.INDICATORS)
    assert len(calls) == 5
    assert {call[2] for call in calls} == {15}
    assert all(call[1] <= 15 and call[3] for call in calls)
    assert len({call[3] for call in calls}) == 5


def test_sidra_refresh_callback_shares_dataset_root_budget(collector_db, monkeypatch) -> None:
    from datafetchers import ibge_sidra

    specs_by_table = {str(spec.table_id): spec for spec in ibge_sidra.SIDRA_DATASETS.values()}
    def response(url: str, _kwargs) -> httpx.Response:
        spec = next(spec for table, spec in specs_by_table.items() if f"/t/{table}/" in url)
        header = {key: key for key in ("NC", "NN", "MC", "MN", "V", "D1C", "D1N", "D2C", "D2N", "D3C", "D3N")}
        header["V"] = "Valor"
        row = {"NC": "1", "NN": "Brasil", "MC": spec.unit_code, "MN": spec.unit_label,
               "V": "Valor", "D1C": "1", "D1N": "Brasil", "D2C": str(spec.variable_id),
               "D2N": spec.variable_label, "D3C": "202601", "D3N": "janeiro 2026"}
        if spec.category_id is not None:
            header.update({"D4C": "D4C", "D4N": "D4N"})
            row.update({"D4C": str(spec.category_id), "D4N": spec.category_label or "category"})
        return _response(url, [header, row])

    calls = _spy_http(monkeypatch, response)
    result = ibge_sidra.refresh_sidra_data()
    assert set(result) == set(ibge_sidra.SIDRA_DATASETS)
    assert len(calls) == 5
    assert {call[2] for call in calls} == {15}
    assert len({call[3] for call in calls}) == 5
    assert all(call[1] <= 15 for call in calls)


def test_b3_public_callback_propagates_budget_into_download_worker(collector_db, monkeypatch) -> None:
    from datafetchers import b3_di

    calls = _spy_http(monkeypatch, lambda url, _kwargs: _response(url, {}, content=b"invalid archive"))
    with pytest.raises(b3_di.InvalidB3ArchiveError):
        b3_di.fetch_di_curves(days=1, use_cache=False)
    assert len(calls) == 1
    assert calls[0][1] == 1
    assert calls[0][2] == 2
    assert calls[0][3] is not None


def test_anbima_public_callback_charges_workbook_http(collector_db, monkeypatch) -> None:
    from datafetchers import anbima

    calls = _spy_http(monkeypatch, lambda url, _kwargs: _response(url, {}, content=b"fixture workbook"))
    workbook = MagicMock()
    workbook.sheet_names = []
    monkeypatch.setattr(anbima.CalamineWorkbook, "from_filelike", lambda _file: workbook)
    result = anbima.fetch_anbima_ima(use_cache=False)
    assert result == {}
    assert len(calls) == 1
    assert calls[0][1:] == (1, 3, "anbima:ima_workbook")


def test_treasury_public_callback_charges_csv_http(collector_db, monkeypatch) -> None:
    import provider_http
    from datafetchers import tesouro_direto

    body = (
        b"Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;PU Compra Manha;PU Venda Manha;PU Base Manha\n"
        b"Tesouro Prefixado;01/01/2030;26/10/2026;10,0;10,1;900;899;901\n"
    )
    observed: list[int] = []

    class CountingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            budget = current_retry_budget()
            assert budget is not None
            observed.append(budget.requests)
            return httpx.Response(200, content=body, request=request)

        async def aclose(self) -> None:
            return None

    original_client = httpx.AsyncClient
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    monkeypatch.setattr(tesouro_direto, "TESOURO_TRANSPARENTE_CSV", "https://fixture.invalid/treasury.csv")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=CountingTransport(), **kwargs))
    result = tesouro_direto.refresh_treasury_data()
    assert result["rows"] == 1
    assert observed == [1]


@pytest.mark.parametrize(
    ("status_code", "category"),
    [(429, "rate_limited"), (503, "temporary_server_error")],
)
def test_treasury_final_retry_after_persists_and_blocks_next_http(
    collector_db, monkeypatch, status_code: int, category: str
) -> None:
    import provider_http
    from datafetchers import tesouro_direto

    attempts: list[int] = []

    class RateLimitedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(
                status_code,
                headers={"Retry-After": "1" if len(attempts) == 1 else "3600"},
                content=b"", request=request,
            )

        async def aclose(self) -> None:
            return None

    original_client = httpx.AsyncClient
    async def skip_provider_backoff(_seconds: float) -> None:
        return None

    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    monkeypatch.setattr(provider_http.asyncio, "sleep", skip_provider_backoff)
    monkeypatch.setattr(tesouro_direto, "TESOURO_TRANSPARENTE_CSV", "https://fixture.invalid/limited.csv")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=RateLimitedTransport(), **kwargs))

    with pytest.raises(RetryDeferred):
        tesouro_direto.refresh_treasury_data()

    extraction = store._query(
        "SELECT error_category, retry_not_before FROM source_extractions ORDER BY started_at DESC LIMIT 1"
    )[0]
    cooldown = datetime.fromisoformat(extraction["retry_not_before"])
    assert extraction["error_category"] == category
    assert cooldown >= datetime.now(timezone.utc) + timedelta(minutes=59)
    state = store._query(
        "SELECT retry_not_before FROM source_access_state WHERE scope_id = ?",
        ("tesouro:primary:primary",),
    )[0]
    assert datetime.fromisoformat(state["retry_not_before"]) == cooldown

    with pytest.raises(CircuitOpen):
        tesouro_direto.refresh_treasury_data()
    assert len(attempts) == 2
