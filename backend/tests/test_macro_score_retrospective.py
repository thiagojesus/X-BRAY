import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Self

import pytest
from fastapi.testclient import TestClient

from db import store
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from macro_score.inputs import ScoreVintage
from macro_score.methodology import COMPONENTS_BY_KEY, MacroComponentKey
from macro_score.models import (
    ComponentPoint,
    ComponentSeries,
    ComponentUnavailableReason,
)
from macro_score.retrospective import (
    DI_FORMULA_VERSION,
    FORMULA_VERSION,
    SOURCE_KEYS,
    _build_rows,
    _date_windows,
    _di_contracts_for_date,
    _fetch_observations,
    _focus_pages,
    _get,
    _insert_rows,
    _load_sgs,
    _month_inputs,
    _paired_values,
    _select_focus_months,
    build_retrospective_di_values,
    eligible_retrospective_di_values,
    overlay_retrospective_history,
    reconstruct,
    reconstruct_di_ledger,
)
from macro_score.sources import (
    DIInstrumentSnapshot,
    MacroScoreSources,
    RetrospectiveDIObservation,
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


def _di_sources(month_count: int) -> MacroScoreSources:
    observations: list[RetrospectiveDIObservation] = []
    snapshots: list[DIInstrumentSnapshot] = []
    vintages: list[ScoreVintage] = []
    fetched = datetime(2026, 10, 20, tzinfo=timezone.utc)
    for index in range(month_count):
        month_index = 2024 * 12 + index
        year, month_zero = divmod(month_index, 12)
        month = month_zero + 1
        day = date(year, month, 25)
        focus_day = date(year, month, 25)
        for short, days, rate in (("DI1Y", 365, 12.0), ("DI5Y", 1825, 14.0)):
            symbol = f"{short}{index:02d}"
            maturity = focus_day + timedelta(days=days)
            observations.append(RetrospectiveDIObservation(
                day, symbol, maturity, rate, f"hash-{index}-{symbol}",
                f"SPRD{index}.zip", f"SPRD{index}.zip!{symbol}.xml",
                fetched, "retrospective_reference", "legacy_local_unverified", f"id-{symbol}",
            ))
            snapshots.append(DIInstrumentSnapshot(
                day, symbol, f"isin-{symbol}", f"id-{symbol}", maturity,
                date(2020, 1, 1), date(2035, 12, 31), f"catalog-{symbol}", "catalog.xml",
                "bvmf.100.02", f"msg-{symbol}", fetched, fetched, "b3_instrument_snapshot_v1",
            ))
        vintages.append(ScoreVintage(
            provider="focus", series_key="IPCA_12M_S_BASE0", reference_period=focus_day,
            value=3.0, unit="pct_annual", source="https://focus.test",
            available_at=fetched, first_seen_at=fetched, payload_hash=f"focus-{index}",
        ))
    return MacroScoreSources(tuple(vintages), (), (), tuple(observations), tuple(snapshots))


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
    # Same fetched_at with a divergent revision is ambiguous; never select by hash.
    assert len(result.history) == 23
    assert result.history[0].reference_period == date(2024, 2, 1)


@pytest.mark.parametrize("months,eligible", [(23, False), (24, True)])
def test_retrospective_di_calculates_fisher_and_slope_with_monthly_coverage(
    months: int, eligible: bool,
) -> None:
    rows, failures = build_retrospective_di_values(_di_sources(months))
    by_component = {key: [row for row in rows if row[0] == key]
                    for key in ("juro_real_ex_ante_1y", "inclinacao_di_5y_1y")}
    assert all(len(component_rows) == months for component_rows in by_component.values())
    assert all(len({row[1] for row in component_rows}) == months
               for component_rows in by_component.values())
    assert all(row[4] == DI_FORMULA_VERSION and row[5] == "retrospective_reference"
               for row in rows)
    assert by_component["juro_real_ex_ante_1y"][0][2] == pytest.approx(
        ((1.12 / 1.03) - 1) * 100
    )
    assert all(row[2] == 2.0 for row in by_component["inclinacao_di_5y_1y"])
    assert (len(by_component["juro_real_ex_ante_1y"]) >= 24) is eligible
    assert failures == {}


def test_di_overlay_requires_24_distinct_months_and_verified_maturity_provenance() -> None:
    sources = _di_sources(24)
    eligible = eligible_retrospective_di_values(sources)
    assert len(eligible) == 48
    unverified = MacroScoreSources(
        sources.vintages, (), (), tuple(
            item.__class__(
                item.trade_date, item.symbol, item.maturity, item.rate,
                item.source_hash, item.source_name, item.source_member,
                item.fetched_at, item.source_basis, "legacy_local_unverified",
            ) for item in sources.retrospective_di
        ), (),
    )
    candidates, failures = build_retrospective_di_values(unverified)
    assert candidates == []
    assert not eligible_retrospective_di_values(unverified)
    assert all("instrument_snapshot_missing_or_conflicting" in failures[month] for month in failures)
    assert not eligible_retrospective_di_values(_di_sources(23))


def test_instrument_identity_expiry_conflict_and_trading_boundaries_fail_closed() -> None:
    sources = _di_sources(1)
    day = sources.retrospective_di[0].trade_date
    snapshots = list(sources.di_instrument_snapshots)
    contracts = _di_contracts_for_date(sources.retrospective_di, day, tuple(snapshots))
    assert contracts is not None and len(contracts) == 2

    wrong_id = tuple(
        snapshot.__class__(
            snapshot.report_date, snapshot.symbol, snapshot.security_id, "other-fin-id",
            snapshot.maturity, snapshot.trading_start, snapshot.trading_end,
            snapshot.source_hash, snapshot.source_name, snapshot.message_definition,
            snapshot.message_id, snapshot.captured_at, snapshot.available_at, snapshot.source_basis,
        ) if snapshot.symbol == sources.retrospective_di[0].symbol else snapshot
        for snapshot in snapshots
    )
    wrong_id_contracts = _di_contracts_for_date(sources.retrospective_di, day, wrong_id)
    assert wrong_id_contracts is not None
    assert sources.retrospective_di[0].symbol not in {item.symbol for item in wrong_id_contracts}

    changed_expiry = snapshots[0].__class__(
        snapshots[0].report_date + timedelta(days=1), snapshots[0].symbol,
        snapshots[0].security_id, snapshots[0].fin_instrm_id,
        snapshots[0].maturity + timedelta(days=1), snapshots[0].trading_start,
        snapshots[0].trading_end, "later-hash", snapshots[0].source_name,
        snapshots[0].message_definition, "later-message", snapshots[0].captured_at,
        snapshots[0].available_at, snapshots[0].source_basis,
    )
    assert _di_contracts_for_date(
        sources.retrospective_di, day, (*snapshots, changed_expiry),
    ) is None

    end_boundary = tuple(
        snapshot.__class__(
            snapshot.report_date, snapshot.symbol, snapshot.security_id, snapshot.fin_instrm_id,
            snapshot.maturity, snapshot.trading_start, day, snapshot.source_hash,
            snapshot.source_name, snapshot.message_definition, snapshot.message_id,
            snapshot.captured_at, snapshot.available_at, snapshot.source_basis,
        ) for snapshot in snapshots
    )
    assert len(_di_contracts_for_date(sources.retrospective_di, day, end_boundary) or ()) == 2
    assert _di_contracts_for_date(
        sources.retrospective_di, day + timedelta(days=1), end_boundary,
    ) == ()


def test_reconstruct_di_ledger_dry_run_and_execute_are_isolated_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from macro_score import retrospective
    from macro_score.retrospective_di import import_local_files

    # Isolated target seeded with a purpose-built 2024-01 curve; no network fixtures.
    target = tmp_path / "isolated-di.db"
    from tests.test_retrospective_di import XML
    source = tmp_path / "sample.xml"
    source.write_bytes(XML)
    import_local_files(target, [source], execute=True,
                       fetched_at=datetime(2026, 10, 20, tzinfo=timezone.utc))
    # The sample contract curve is intentionally insufficient; the dry-run still
    # returns explicit gap reasons and does not create retrospective macro rows.
    monkeypatch.setattr(retrospective, "load_score_vintages", lambda *, as_of: ())
    dry = reconstruct_di_ledger(target, as_of=datetime(2026, 10, 21, tzinfo=timezone.utc))
    assert dry["dry_run"] is True and dry["inserted"] == 0
    assert "juro_real_ex_ante_1y" not in dry["counts"]
    assert dry["failures_by_month"]["2026-10"]["focus_12m_base0_missing"] == 1
    first = reconstruct_di_ledger(
        target, as_of=datetime(2026, 10, 21, tzinfo=timezone.utc), execute=True,
    )
    second = reconstruct_di_ledger(
        target, as_of=datetime(2026, 10, 21, tzinfo=timezone.utc), execute=True,
    )
    assert first["inserted"] == second["inserted"] == 0
    assert first["source_basis"] == "retrospective_reference"


def test_retrospective_di_aligns_before_future_focus_and_skips_conflicting_revision() -> None:
    sources = _di_sources(1)
    di = list(sources.retrospective_di)
    focus = list(sources.vintages)
    # The last DI curve date precedes this month's last Focus reference: no later curve is used.
    focus[0] = focus[0].model_copy(update={"reference_period": date(2024, 1, 26)})
    earlier_focus = focus[0].model_copy(update={
        "reference_period": date(2024, 1, 24), "payload_hash": "focus-earlier",
    })
    aligned = MacroScoreSources((*focus, earlier_focus), (), (), tuple(di), sources.di_instrument_snapshots)
    rows, _failures = build_retrospective_di_values(aligned)
    assert {row[1] for row in rows} == {"2024-01"}
    assert all('"reference_date":"2024-01-25"' in str(row[7]) for row in rows)

    conflict = RetrospectiveDIObservation(
        di[0].trade_date, di[0].symbol, di[0].maturity, di[0].rate + 1,
        "different-hash", "revision.xml", "revision.xml", di[0].fetched_at,
        "retrospective_reference",
    )
    conflicting = MacroScoreSources(tuple(sources.vintages), (), (), (*di, conflict), sources.di_instrument_snapshots)
    no_rows, reasons = build_retrospective_di_values(conflicting)
    assert no_rows == []
    assert reasons["2024-01"]["conflicting_di_revision"] == 1


def test_di_overlay_uses_real_fetch_timestamp_and_does_not_backdate_or_mix_tied_revisions() -> None:
    spec = COMPONENTS_BY_KEY[MacroComponentKey("juro_real_ex_ante_1y")]
    empty = ComponentSeries(spec.key, None, (), ComponentUnavailableReason.REQUIRED_INPUT_MISSING)
    fetched = "2026-10-20T00:00:00+00:00"
    source_json = json.dumps({"di_1y": {
        "symbol": "DI1X24", "fin_instrm_id": "200001", "instrument_isin": "BRISIN",
        "maturity_basis": "b3_instrument_snapshot_v1", "expiry_field": "FutrCtrctsInf/XprtnDt",
        "instrument_source_hash": "catalog-hash", "instrument_report_date": "2026-10-20",
        "instrument_source_basis": "b3_instrument_snapshot_v1",
        "instrument_source_name": "catalog.xml",
        "instrument_available_at": fetched, "security_id": "200001", "trade_date": "2024-01-25",
        "maturity": "2025-01-02", "rate": 12.0,
    }})
    store.execute_change(
        "INSERT INTO b3_di_instrument_snapshots "
        "(snapshot_date,report_date,symbol,security_id,fin_instrm_id,maturity,trading_start,trading_end,"
        "source_hash,source_name,message_definition,message_id,captured_at,available_at,source_basis) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-10-20", "2026-10-20", "DI1X24", "BRISIN", "200001", "2025-01-02",
         "2020-01-01", "2025-01-01", "catalog-hash", "catalog.xml", "bvmf.100.02", "msg-1",
         fetched, fetched, "b3_instrument_snapshot_v1"),
    )
    value_rows = []
    for index in range(24):
        year, month_zero = divmod(2024 * 12 + index, 12)
        value_rows.append((
            "juro_real_ex_ante_1y", f"{year:04d}-{month_zero + 1:02d}",
            1.5, "pct_annual", DI_FORMULA_VERSION, "retrospective_reference",
            fetched, source_json, f"hash-{index}", None,
        ))
    value_row = value_rows[0]
    retro = overlay_retrospective_history(
        (empty,), as_of=datetime(2026, 10, 21, tzinfo=timezone.utc),
        additional_values=value_rows,
    )[0]
    assert len(retro.history) == 24
    assert retro.history[0].available_at == datetime.fromisoformat(fetched)
    assert dict(retro.history[0].inputs[0].metadata)["formula_version"] == DI_FORMULA_VERSION
    forward_point = ComponentPoint(
        value=99.0, unit=spec.unit, reference_period=date(2023, 12, 1),
        available_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        input_fingerprint="forward", inputs=(),
    )
    mixed = ComponentSeries(spec.key, None, (forward_point,), ComponentUnavailableReason.REQUIRED_INPUT_MISSING)
    replacement = overlay_retrospective_history(
        (mixed,), as_of=datetime(2026, 10, 21, tzinfo=timezone.utc),
        additional_values=value_rows,
    )[0]
    assert len(replacement.history) == 24
    before_fetch = overlay_retrospective_history(
        (empty,), as_of=datetime(2026, 10, 19, tzinfo=timezone.utc),
        additional_values=value_rows,
    )[0]
    assert before_fetch.history == ()
    collision = (*value_row[:2], 2.0, *value_row[3:8], "hash-b", None)
    ambiguous = overlay_retrospective_history(
        (empty,), as_of=datetime(2026, 10, 21, tzinfo=timezone.utc),
        additional_values=[*value_rows, collision],
    )[0]
    assert len(ambiguous.history) == 23
    assert all(point.reference_period != date(2024, 1, 1) for point in ambiguous.history)


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


def test_retrospective_api_reports_di_formula_metadata_and_only_eligible_pairs() -> None:
    rows = []
    for component in ("juro_real_ex_ante_1y", "inclinacao_di_5y_1y"):
        rows.append((
            component, "2025-01", 1.0, "pp", DI_FORMULA_VERSION,
            "retrospective_reference", "2026-10-20T00:00:00+00:00",
            '{"reference_date":"2025-01-30"}', f"hash-{component}", None,
        ))
    _insert_rows(store._conn, rows)
    from main import app

    with TestClient(app) as client:
        response = client.get("/api/macro-score/retrospective-history?start=2025-01&end=2025-01")
    body = response.json()
    assert body["di_history_available"] is True
    assert DI_FORMULA_VERSION in body["formula_versions"]
    assert body["coverage"]["juro_real_ex_ante_1y"]["percentile_eligible"] is False
    assert all(item["inputs"]["reference_date"] == "2025-01-30" for item in body["data"])


def test_api_reconstructs_di_rows_from_isolated_ledger_without_network() -> None:
    from tests.test_macro_score_sources import _insert_vintage, _VintageSeed

    source = _di_sources(1)
    for observation in source.retrospective_di:
        store.execute_change(
            "INSERT INTO retrospective_di_observations "
            "(trade_date,symbol,maturity,rate,source_hash,fetched_at,revision,source_basis,source_name,source_member) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (observation.trade_date.isoformat(), observation.symbol,
             observation.maturity.isoformat(), observation.rate, observation.source_hash,
             observation.fetched_at.isoformat(), "revision", observation.source_basis,
             observation.source_name, observation.source_member),
            )
    for instrument in source.di_instrument_snapshots:
        store.execute_change(
            "INSERT INTO b3_di_instrument_snapshots "
            "(snapshot_date,report_date,symbol,security_id,fin_instrm_id,maturity,trading_start,trading_end,"
            "source_hash,source_name,message_definition,message_id,captured_at,available_at,source_basis) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (instrument.report_date.isoformat(), instrument.report_date.isoformat(), instrument.symbol,
             instrument.security_id, instrument.fin_instrm_id, instrument.maturity.isoformat(),
             instrument.trading_start.isoformat(), instrument.trading_end.isoformat(), instrument.source_hash,
             instrument.source_name, instrument.message_definition, instrument.message_id,
             instrument.captured_at.isoformat(), instrument.available_at.isoformat(), instrument.source_basis),
        )
    _insert_vintage(_VintageSeed(
        "focus", "IPCA_12M_S_BASE0", "2024-01-25",
        {"Indicador": "IPCA", "Data": "2024-01-25", "Mediana": 3.0,
         "Suavizada": "S", "baseCalculo": 0},
        datetime(2026, 10, 20, tzinfo=timezone.utc),
    ))
    from main import app

    with TestClient(app) as client:
        response = client.get("/api/macro-score/retrospective-history?start=2024-01&end=2024-01")
    body = response.json()
    assert body["di_history_available"] is True
    assert {record["component_key"] for record in body["data"]} == {
        "juro_real_ex_ante_1y", "inclinacao_di_5y_1y",
    }
    assert all(record["point_in_time"] is False for record in body["data"])


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
