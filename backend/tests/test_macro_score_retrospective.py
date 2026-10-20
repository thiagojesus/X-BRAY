from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Self

import pytest
from fastapi.testclient import TestClient

from db import store
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from macro_score.methodology import COMPONENTS_BY_KEY, MacroComponentKey
from macro_score.models import ComponentSeries, ComponentUnavailableReason
from macro_score.retrospective import (
    FORMULA_VERSION,
    SOURCE_KEYS,
    _build_rows,
    _date_windows,
    _fetch_observations,
    _focus_pages,
    _get,
    _insert_rows,
    _load_sgs,
    _month_inputs,
    _paired_values,
    _select_focus_months,
    overlay_retrospective_history,
    reconstruct,
)


@pytest.fixture(autouse=True)
def isolated_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "retro.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _rows(count: int) -> list[tuple[object, ...]]:
    rows = []
    for index in range(count):
        month_index = 2024 * 12 + index
        year, month = divmod(month_index, 12)
        reference = f"{year:04d}-{month + 1:02d}"
        source_json = '{"source":"sgs"}'
        rows.append(
            (
                "desemprego",
                reference,
                float(index),
                "pct",
                FORMULA_VERSION,
                "retrospective_reference",
                "2026-10-20T00:00:00+00:00",
                source_json,
                f"hash-{index}",
                None,
            )
        )
    return rows


def test_retrospective_history_overlay_counts_distinct_months_and_preserves_cutoff() -> (
    None
):
    _insert_rows(store._conn, _rows(24))
    spec = COMPONENTS_BY_KEY[MacroComponentKey("desemprego")]
    empty = ComponentSeries(
        spec.key, None, (), ComponentUnavailableReason.REQUIRED_INPUT_MISSING
    )
    overlaid = overlay_retrospective_history(
        (empty,), as_of=datetime(2026, 10, 21, tzinfo=timezone.utc)
    )[0]
    assert len(overlaid.history) == 24
    assert all(
        dict(point.inputs[0].metadata)["source_basis"] == "retrospective_reference"
        for point in overlaid.history
    )
    before_capture = overlay_retrospective_history(
        (empty,), as_of=datetime(2026, 10, 19, tzinfo=timezone.utc)
    )[0]
    assert before_capture.history == ()


def test_retrospective_history_deduplicates_revisions_by_month() -> None:
    rows = _rows(24)
    revised = (*rows[0][:2], 999.0, *rows[0][3:-2], "revision", None)
    _insert_rows(store._conn, [*rows, revised])
    spec = COMPONENTS_BY_KEY[MacroComponentKey("desemprego")]
    empty = ComponentSeries(
        spec.key, None, (), ComponentUnavailableReason.REQUIRED_INPUT_MISSING
    )
    result = overlay_retrospective_history(
        (empty,), as_of=datetime(2026, 10, 21, tzinfo=timezone.utc)
    )[0]
    assert len(result.history) == 24
    assert result.history[0].value == 999.0


def test_retrospective_api_is_separate_from_production_history() -> None:
    from main import app

    _insert_rows(store._conn, _rows(23))
    with TestClient(app) as client:
        response = client.get(
            "/api/macro-score/retrospective-history?start=2024-01&end=2026-12"
        )
    assert response.status_code == 200
    body = response.json()
    assert body["source_basis"] == "retrospective_reference"
    assert body["point_in_time"] is False
    assert body["coverage"]["desemprego"] == {
        "distinct_months": 23,
        "percentile_eligible": False,
    }
    assert body["di_history_available"] is False


def test_reconstruction_target_and_date_bounds_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute local"):
        reconstruct(Path("relative.db"), start=date(2024, 1, 1), end=date(2024, 2, 1))
    with pytest.raises(ValueError, match="ordered"):
        reconstruct(tmp_path / "local.db", start=date(2024, 2, 1), end=date(2024, 1, 1))


def test_focus_selection_keeps_latest_finite_record_per_month() -> None:
    records = [
        {
            "Indicador": "IPCA",
            "Suavizada": "S",
            "baseCalculo": 0,
            "Data": "2024-01-02",
            "Mediana": 3.0,
        },
        {
            "Indicador": "IPCA",
            "Suavizada": "S",
            "baseCalculo": 0,
            "Data": "2024-01-31",
            "Mediana": 4.0,
        },
        {
            "Indicador": "IPCA",
            "Suavizada": "S",
            "baseCalculo": 0,
            "Data": "2024-02-01",
            "Mediana": float("nan"),
        },
        {
            "Indicador": "IPCA",
            "Suavizada": "S",
            "baseCalculo": 1,
            "Data": "2024-02-02",
            "Mediana": 5.0,
        },
    ]
    selected = _select_focus_months(records, date(2024, 1, 1), date(2024, 2, 29))
    assert selected == {"2024-01": (4.0, records[1])}


def test_focus_rejects_a_reference_after_current_cutoff() -> None:
    future = [
        {
            "Indicador": "IPCA",
            "Suavizada": "S",
            "baseCalculo": 0,
            "Data": "2026-10-20",
            "Mediana": 3.0,
        }
    ]
    with pytest.raises(ValueError, match="exceeds current cutoff"):
        _select_focus_months(
            future, date(2026, 1, 1), date(2026, 12, 31), cutoff=date(2026, 10, 19)
        )


def test_focus_pagination_refuses_untrusted_continuation() -> None:
    class Response:
        content = b"{}"
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"value": [], "@odata.nextLink": "https://example.invalid/page"}

    class Client:
        def get(self, _url: str, **_kwargs: object) -> Response:
            return Response()

    with pytest.raises(ValueError, match="official BCB host"):
        _focus_pages(Client())  # type: ignore[arg-type]


def test_reconstruction_produces_paired_and_targeted_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from macro_score import retrospective

    end = date(2026, 3, 31)
    start = date(2023, 3, 1)
    specs = {
        key: INDICATOR_CATALOG[IndicatorKey(key)]
        for key in SOURCE_KEYS | {"idp_12m_pib"}
    }
    code_to_key = {int(spec.provider_code): key for key, spec in specs.items()}

    def fake_sgs(
        _client: object, code: int, window_start: date, window_end: date
    ) -> list[dict[str, object]]:
        key = code_to_key[code]
        value = -2.0 if key == "conta_corrente_12m_pib" else 7.0
        dates = [date(year, 3, 1) for year in range(2023, 2027)]
        return [
            {"data": point.strftime("%d/%m/%Y"), "valor": str(value)}
            for point in dates
            if window_start <= point <= window_end
        ]

    focus_rows = [
        {
            "Indicador": "IPCA",
            "Suavizada": "S",
            "baseCalculo": 0,
            "Data": f"2026-03-{day:02d}",
            "Mediana": 3.7,
        }
        for day in (2, 30)
    ]
    monkeypatch.setattr(retrospective, "_load_sgs", fake_sgs)
    monkeypatch.setattr(retrospective, "_focus_pages", lambda _client: focus_rows)
    report = reconstruct(tmp_path / "only-local.db", start=start, end=end)
    assert report["dry_run"] is True and report["focus_months"] == 1
    assert "spread_credito_pf_selic" in report["counts"]
    assert "cobertura_externa" in report["counts"]
    monthly = _month_inputs(
        {
            "taxa_media_credito_pf": {"2026-03": (22.0, {})},
            "selic_meta": {"2026-03": (10.0, {})},
            "conta_corrente_12m_pib": {"2026-03": (-2.0, {})},
            "idp_12m_pib": {"2026-03": (4.0, {})},
        }
    )
    paired = _paired_values(monthly["2026-03"])
    assert paired["spread_credito_pf_selic"][0] == 12.0
    assert paired["cobertura_externa"][0] == 2.0
    assert _build_rows(monthly, {}, "2026-10-20T00:00:00+00:00")


@pytest.mark.parametrize(
    ("returned_date", "window_start", "window_end", "message"),
    [
        (
            date(2020, 1, 1),
            date(2020, 1, 2),
            date(2020, 1, 31),
            "outside requested window",
        ),
        (
            date(2020, 2, 1),
            date(2020, 1, 1),
            date(2020, 1, 31),
            "outside requested window",
        ),
        (
            date(2010, 12, 31),
            date(2011, 1, 1),
            date(2021, 1, 1),
            "outside requested window",
        ),
    ],
)
def test_sgs_rejects_rows_before_or_after_each_requested_window(
    monkeypatch: pytest.MonkeyPatch,
    returned_date: date,
    window_start: date,
    window_end: date,
    message: str,
) -> None:
    from macro_score import retrospective

    class DummyClient:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(retrospective.httpx, "Client", lambda **_kwargs: DummyClient())
    monkeypatch.setattr(retrospective, "_focus_pages", lambda _client: [])
    monkeypatch.setattr(
        retrospective,
        "_date_windows",
        lambda _start, _end: ((window_start, window_end),),
    )
    monkeypatch.setattr(
        retrospective,
        "_load_sgs",
        lambda *_args: [{"data": returned_date.strftime("%d/%m/%Y"), "valor": "1"}],
    )
    with pytest.raises(ValueError, match=message):
        _fetch_observations(date(2020, 1, 1), date(2021, 1, 1))


@pytest.mark.parametrize("returned_date", [date(2018, 12, 31), date(2021, 2, 2)])
def test_sgs_rejects_rows_outside_overall_input_and_output_bounds(
    monkeypatch: pytest.MonkeyPatch, returned_date: date
) -> None:
    from macro_score import retrospective

    class DummyClient:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(retrospective.httpx, "Client", lambda **_kwargs: DummyClient())
    monkeypatch.setattr(retrospective, "_focus_pages", lambda _client: [])
    monkeypatch.setattr(
        retrospective,
        "_date_windows",
        lambda _start, _end: ((date(2018, 12, 1), date(2021, 2, 28)),),
    )
    monkeypatch.setattr(
        retrospective,
        "_load_sgs",
        lambda *_args: [{"data": returned_date.strftime("%d/%m/%Y"), "valor": "1"}],
    )
    with pytest.raises(
        ValueError,
        match="outside requested window|outside allowed reconstruction range",
    ):
        _fetch_observations(date(2020, 1, 1), date(2021, 1, 1))


def test_sgs_rejects_future_reference_even_if_it_is_inside_malicious_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from macro_score import retrospective

    today = datetime.now(timezone.utc).date()
    future = today + timedelta(days=1)

    class DummyClient:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(retrospective.httpx, "Client", lambda **_kwargs: DummyClient())
    monkeypatch.setattr(
        retrospective, "_date_windows", lambda _start, _end: ((today, future),)
    )
    monkeypatch.setattr(
        retrospective,
        "_load_sgs",
        lambda *_args: [{"data": future.strftime("%d/%m/%Y"), "valor": "1"}],
    )
    with pytest.raises(ValueError, match="outside allowed reconstruction range"):
        _fetch_observations(today, future)


def test_twelve_month_input_buffer_calculates_requested_output_without_emitting_buffer() -> (
    None
):
    monthly = {
        "2023-12": {"ibc_br_sa": (100.0, {"record": "prior"})},
        "2024-12": {"ibc_br_sa": (110.0, {"record": "current"})},
    }
    rows = _build_rows(
        monthly,
        {},
        "2026-10-20T00:00:00+00:00",
        output_start=date(2024, 12, 1),
        output_end=date(2024, 12, 31),
    )
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("ibc_br_sa_yoy", "2024-12", 10.000000000000009)
    ]


def test_focus_pagination_follows_official_link_and_collects_all_pages() -> None:
    class Response:
        status_code = 200
        content = b"{}"

        def __init__(self, value: dict[str, object]) -> None:
            self.value = value

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self.value

    class Client:
        calls = 0

        def get(self, _url: str, **_kwargs: object) -> Response:
            self.calls += 1
            if self.calls == 1:
                return Response(
                    {
                        "value": [{"Data": "2024-01-01"}],
                        "@odata.nextLink": "https://olinda.bcb.gov.br/next",
                    }
                )
            return Response({"value": [{"Data": "2024-02-01"}]})

    client = Client()
    assert [row["Data"] for row in _focus_pages(client)] == ["2024-01-01", "2024-02-01"]


def test_get_retries_transient_connect_failure_and_rejects_oversized_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from macro_score import retrospective

    class Response:
        status_code = 200
        content = b"{}"

        def raise_for_status(self) -> None:
            return None

    class Client:
        calls = 0

        def get(self, _url: str, **_kwargs: object) -> Response:
            self.calls += 1
            if self.calls == 1:
                raise retrospective.httpx.ConnectError("temporary")
            return Response()

    monkeypatch.setattr(retrospective.time, "sleep", lambda _seconds: None)
    client = Client()
    assert _get(client, "https://bcb.example") is not None
    assert client.calls == 2
    monkeypatch.setattr(retrospective, "JSON_MAX_BYTES", 1)
    with pytest.raises(ValueError, match="size limit"):
        _get(Client(), "https://bcb.example")


def test_sgs_parser_returns_empty_for_official_no_values_and_validates_body() -> None:
    class Response:
        content = b"{}"

        def __init__(self, status: int, value: object) -> None:
            self.status_code = status
            self.value = value

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError("http failure")

        def json(self) -> object:
            return self.value

    class Client:
        response: Response

        def get(self, _url: str, **_kwargs: object) -> Response:
            return self.response

    client = Client()
    client.response = Response(404, {"erro": {"detail": "Value(s) not found"}})
    assert _load_sgs(client, 123, date(2024, 1, 1), date(2024, 2, 1)) == []
    client.response = Response(200, [{"data": "01/01/2024", "valor": "1"}, None])
    with pytest.raises(TypeError, match="non-object observation"):
        _load_sgs(client, 123, date(2024, 1, 1), date(2024, 2, 1))
    client.response = Response(200, {})
    with pytest.raises(TypeError, match="not a list"):
        _load_sgs(client, 123, date(2024, 1, 1), date(2024, 2, 1))


def test_date_window_split_and_execute_backfill_are_bounded_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from macro_score import retrospective

    start, end = date(2016, 10, 1), date(2026, 10, 19)
    assert _date_windows(start, end) == (
        (start, date(2026, 10, 1)),
        (date(2026, 10, 1), end),
    )
    monkeypatch.setattr(
        retrospective, "_fetch_observations", lambda _start, _end: ({}, {})
    )
    target = tmp_path / "exec.db"
    first = reconstruct(target, start=start, end=end, dry_run=False)
    second = reconstruct(target, start=start, end=end, dry_run=False)
    assert first["inserted"] == second["inserted"] == 0
    assert first["counts"] == {}
