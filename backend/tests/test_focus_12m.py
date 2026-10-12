from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import httpx
import pytest

from datafetchers import focus
from db import store
from refresh import RefreshDependencies, daily_refresh

FOCUS_12M_KEY = "IPCA_12M_S_BASE0"
VALID_RECORD = {
    "Indicador": "IPCA",
    "Data": "2026-09-25",
    "Mediana": 4.66,
    "Suavizada": "S",
    "baseCalculo": 0,
}


@pytest.fixture(autouse=True)
def isolated_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "focus-12m.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _response(records: list[dict[str, object]]) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"value": records}
    return response


def test_red_xbr_502_daily_refresh_registers_focus_12m_once() -> None:
    # Given
    payload = {"data": [{"value": 1}]}
    dependencies = RefreshDependencies(
        fetch_sgs_batch=lambda *_args, **_kwargs: payload,
        fetch_all_focus=lambda **_kwargs: payload,
        fetch_focus_12m=lambda **_kwargs: payload,
        fetch_anbima_ima=lambda **_kwargs: payload,
        fetch_di_curves=lambda **_kwargs: payload,
        fetch_state_polls=lambda **_kwargs: payload,
        refresh_treasury_data=lambda: payload,
        refresh_sidra_data=lambda: payload,
        refresh_derived_snapshots=lambda _as_of, _cycle_id: 0,
    )

    # When
    result = daily_refresh(force=True, dependencies=dependencies)

    # Then
    task_names = tuple(task["name"] for task in result["tasks"])
    assert task_names.count("focus_12m") == 1, (
        "[RED XBR-502] daily refresh must expose exactly one focus_12m task"
    )


def test_focus_12m_uses_exact_source_contract_and_first_seen_availability() -> None:
    # Given
    store.upsert_focus("IPCA", [{"Data": "2026-09-25", "Mediana": 5.25}])

    # When
    requested_urls: list[httpx.URL] = []

    def request(
        url: str,
        *,
        params: dict[str, str],
        timeout: float,
        follow_redirects: bool,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        del timeout, follow_redirects, headers
        request = httpx.Request("GET", url, params=params)
        requested_urls.append(request.url)
        return httpx.Response(200, json={"value": [VALID_RECORD]}, request=request)

    with patch.object(focus.httpx, "get", side_effect=request):
        result = focus.fetch_focus_12m(use_cache=False)

    # Then
    assert result == {FOCUS_12M_KEY: [VALID_RECORD]}
    assert requested_urls[0].path.endswith("/ExpectativasMercadoInflacao12Meses")
    query = parse_qs(requested_urls[0].query.decode())
    assert query["$filter"] == [
        "Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0",
    ]
    assert query["$orderby"] == ["Data desc"]
    assert query["$top"] == ["200"]
    assert query["$format"] == ["json"]
    assert store.query_focus(FOCUS_12M_KEY) == {FOCUS_12M_KEY: [VALID_RECORD]}
    assert store.query_focus("IPCA") == {
        "IPCA": [{"Data": "2026-09-25", "Mediana": 5.25}],
    }
    vintages = store.query_observation_vintages("focus", FOCUS_12M_KEY)
    assert len(vintages) == 1
    assert vintages[0]["source_published_at"] is None
    assert vintages[0]["available_at"] == vintages[0]["first_seen_at"]


def test_focus_12m_uses_its_own_fresh_cache_without_provider_access() -> None:
    store.upsert_focus(FOCUS_12M_KEY, [VALID_RECORD])
    store.set_meta(
        focus.FOCUS_12M_META_KEY,
        datetime.now(timezone.utc).isoformat(),
    )

    with patch.object(
        focus,
        "get_provider_response",
        side_effect=AssertionError("network access"),
    ):
        result = focus.fetch_focus_12m(use_cache=True)

    assert result == {FOCUS_12M_KEY: [VALID_RECORD]}


@pytest.mark.parametrize(
    "records",
    [
        [],
        [{**VALID_RECORD, "Suavizada": "N"}],
        [{**VALID_RECORD, "baseCalculo": 1}],
        [{**VALID_RECORD, "Mediana": "not-a-number"}],
        [{**VALID_RECORD, "Data": "2026-02-31"}],
    ],
)
def test_invalid_focus_12m_payload_preserves_previous_cache(
    records: list[dict[str, object]],
) -> None:
    # Given
    previous = {**VALID_RECORD, "Data": "2026-09-24", "Mediana": 4.5}
    store.upsert_focus(FOCUS_12M_KEY, [previous])
    before = store.query_observation_vintages("focus", FOCUS_12M_KEY)

    # When
    with patch.object(
        focus,
        "get_provider_response",
        return_value=_response(records),
    ):
        with pytest.raises(RuntimeError):
            focus.fetch_focus_12m(use_cache=False)

    # Then
    assert focus.read_focus_12m() == {FOCUS_12M_KEY: [previous]}
    assert store.query_observation_vintages("focus", FOCUS_12M_KEY) == before


@pytest.mark.parametrize(
    "records",
    [
        [{**VALID_RECORD, "Media": float("nan")}],
        [{**VALID_RECORD, "nested": {"rate": float("inf")}}],
        [{**VALID_RECORD, "nested": [float("-inf")]}],
        [{**VALID_RECORD, "optional": object()}],
        [VALID_RECORD, {**VALID_RECORD, "Media": float("nan")}],
        [{**VALID_RECORD, "Media": float("nan")}, VALID_RECORD],
    ],
)
def test_focus_12m_rejects_non_json_records_before_persistence(
    records: list[dict[str, object]],
) -> None:
    # Given
    previous = {**VALID_RECORD, "Data": "2026-09-24", "Mediana": 4.5}
    store.upsert_focus(FOCUS_12M_KEY, [previous])
    before_vintages = store.query_observation_vintages("focus", FOCUS_12M_KEY)
    previous_marker = "2026-10-06T12:00:00+00:00"
    store.set_meta(focus.FOCUS_12M_META_KEY, previous_marker)

    # When
    with (
        patch.object(focus, "get_provider_response", return_value=_response(records)),
        patch.object(focus, "upsert_focus") as upsert,
        pytest.raises(focus.Focus12MRefreshError, match="invalid_payload"),
    ):
        focus.fetch_focus_12m(use_cache=False)

    # Then
    upsert.assert_not_called()
    assert focus.read_focus_12m() == {FOCUS_12M_KEY: [previous]}
    assert store.query_observation_vintages("focus", FOCUS_12M_KEY) == before_vintages
    assert store.get_meta(focus.FOCUS_12M_META_KEY) == previous_marker


def test_focus_12m_round_trips_valid_optional_nested_json() -> None:
    # Given
    record: dict[str, object] = {
        **VALID_RECORD,
        "optional": None,
        "nested": {"rates": [1, 2.5, True]},
    }

    # When
    with patch.object(focus, "get_provider_response", return_value=_response([record])):
        result = focus.fetch_focus_12m(use_cache=False)

    # Then
    assert result == {FOCUS_12M_KEY: [record]}


def test_focus_12m_timeout_preserves_previous_cache() -> None:
    # Given
    previous = {**VALID_RECORD, "Data": "2026-09-24", "Mediana": 4.5}
    store.upsert_focus(FOCUS_12M_KEY, [previous])

    # When
    with patch.object(
        focus,
        "get_provider_response",
        side_effect=httpx.TimeoutException("provider timeout"),
    ):
        with pytest.raises(RuntimeError):
            focus.fetch_focus_12m(use_cache=False)

    # Then
    assert focus.read_focus_12m() == {FOCUS_12M_KEY: [previous]}


def test_focus_12m_failure_preserves_cache_and_marks_refresh_failed() -> None:
    # Given
    previous = {**VALID_RECORD, "Data": "2026-09-24", "Mediana": 4.5}
    store.upsert_focus(FOCUS_12M_KEY, [previous])
    previous_success = "2026-10-06T12:00:00+00:00"
    store.record_refresh_task_start("focus_12m", "focus", 1, previous_success)
    store.record_refresh_task_finish(
        "focus_12m", "ok", 1, 1, 1, None, previous_success,
    )
    payload = {"data": [{"value": 1}]}
    derived_saw_cache = False

    def derive(_as_of: datetime, _cycle_id: str) -> int:
        nonlocal derived_saw_cache
        derived_saw_cache = focus.read_focus_12m() == {FOCUS_12M_KEY: [previous]}
        return 0

    dependencies = RefreshDependencies(
        fetch_sgs_batch=lambda *_args, **_kwargs: payload,
        fetch_all_focus=lambda **_kwargs: payload,
        fetch_focus_12m=focus.fetch_focus_12m,
        fetch_anbima_ima=lambda **_kwargs: payload,
        fetch_di_curves=lambda **_kwargs: payload,
        fetch_state_polls=lambda **_kwargs: payload,
        refresh_treasury_data=lambda: payload,
        refresh_sidra_data=lambda: payload,
        refresh_derived_snapshots=derive,
    )

    # When
    with patch.object(
        focus,
        "get_provider_response",
        return_value=_response([{**VALID_RECORD, "Media": float("nan")}]),
    ):
        result = daily_refresh(force=True, dependencies=dependencies)

    # Then
    focus_task = next(task for task in result["tasks"] if task["name"] == "focus_12m")
    persisted_task = next(task for task in store.list_refresh_tasks() if task["task"] == "focus_12m")
    assert focus_task == {
        "name": "focus_12m",
        "status": "error",
        "error_code": "Focus12MRefreshError",
    }
    assert persisted_task["last_success"] == previous_success
    assert derived_saw_cache is True
