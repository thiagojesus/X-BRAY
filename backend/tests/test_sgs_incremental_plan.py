from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from db import sgs_refresh_plan, store
from db.sgs_refresh_plan import (
    MAX_ARCHIVE_UNITS_PER_RUN,
    CoverageReceipt,
    SGSUnitCommitReceipt,
    build_next_sgs_plan,
    build_sgs_plan,
    freeze_sgs_plan,
    pending_archive_units,
    record_verified_coverage,
    set_archive_unit_state,
    verified_coverage,
)
from tests.real_rate_support import isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


def test_plan_inventory_uses_bounded_receipt_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    query = store._query

    def tracked(sql: str, params=()):
        calls.append(sql)
        return query(sql, params)

    monkeypatch.setattr(store, "_query", tracked)
    plan = build_next_sgs_plan(date(2026, 10, 27))
    assert len(plan.units) == 68
    coverage_queries = [sql for sql in calls if "FROM sgs_coverage_receipts" in sql]
    assert len(coverage_queries) == 1
    assert "series_code IN" in coverage_queries[0]
    assert len(calls) == 3  # cursor, all coverage receipts, pending archive intent


def test_plan_has_one_recent_unit_per_approved_series_and_four_global_archive_units() -> None:
    plan = build_sgs_plan(date(2026, 10, 27))
    recent = [unit for unit in plan.units if unit.role == "recent"]
    archives = [unit for unit in plan.units if unit.role != "recent"]

    assert len(recent) == len({unit.series_code for unit in recent}) == 64
    assert len(archives) == MAX_ARCHIVE_UNITS_PER_RUN == 4
    assert all(unit.ordinal < len(recent) for unit in recent)
    assert all(unit.ordinal >= len(recent) for unit in archives)
    assert all((unit.end_date - unit.start_date).days <= 365 for unit in archives)
    assert len(plan.units) == 68


def test_recent_calendar_boundaries_and_tr_full_period_override() -> None:
    plan = build_sgs_plan(date(2024, 2, 29), max_archive_units=0)
    by_code = {unit.series_code: unit for unit in plan.units}

    # Daily series use a 90-calendar-day lookback inclusive of the frozen as_of.
    assert by_code[432].start_date == date(2023, 12, 1)
    assert by_code[432].end_date == date(2024, 2, 29)
    # TR remains a full-period identity while its acquisition window is only 90 days.
    assert by_code[226].start_date == date(2023, 12, 1)
    assert by_code[226].identity_kind == "full_period"
    assert by_code[226].method == "full_period"
    monthly = next(unit for unit in plan.units if unit.frequency == "monthly")
    assert monthly.start_date == date(2022, 2, 1)


def test_recent_monthly_quarterly_and_annual_windows_align_to_calendar() -> None:
    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=0)
    units = {unit.series_code: unit for unit in plan.units}
    assert units[188].start_date == date(2024, 10, 1)
    assert units[226].start_date == date(2026, 7, 29)
    from indicator_catalog import Frequency

    annual_spec = replace(next(iter(sgs_refresh_plan.INDICATOR_CATALOG.values())),
                          frequency=Frequency.ANNUAL)
    quarter_spec = replace(next(iter(sgs_refresh_plan.INDICATOR_CATALOG.values())),
                           frequency=Frequency.QUARTERLY)
    moving_quarter_spec = replace(next(iter(sgs_refresh_plan.INDICATOR_CATALOG.values())),
                                  frequency=Frequency.MOVING_QUARTER)
    monthly_spec = next(spec for spec in sgs_refresh_plan.INDICATOR_CATALOG.values()
                        if spec.frequency.value == "monthly")
    assert sgs_refresh_plan._recent_start(annual_spec, date(2026, 10, 27)) == date(2020, 1, 1)
    assert sgs_refresh_plan._recent_start(quarter_spec, date(2026, 10, 27)) == date(2024, 10, 1)
    assert sgs_refresh_plan._recent_start(monthly_spec, date(2024, 3, 31)) == date(2022, 3, 1)
    assert sgs_refresh_plan._recent_start(moving_quarter_spec, date(2024, 3, 31)) == date(2022, 3, 1)
    assert sgs_refresh_plan._recent_start(quarter_spec, date(2024, 2, 29)) == date(2022, 1, 1)
    assert sgs_refresh_plan._recent_start(quarter_spec, date(2024, 4, 1)) == date(2022, 4, 1)
    assert sgs_refresh_plan._recent_start(monthly_spec, date(2026, 2, 28)) == date(2024, 2, 1)
    assert sgs_refresh_plan._recent_start(annual_spec, date(2024, 2, 29)) == date(2018, 1, 1)
    assert units[188].end_date == date(2026, 10, 27)
    pib = units[1207]
    assert (pib.start_date, pib.end_date) == (date(2020, 1, 1), date(2026, 10, 27))
    assert (pib.contract_version, pib.unit, pib.frequency) == (
        "acquisition-contract-v2", "brl", "annual"
    )


def test_legacy_pib_coverage_is_readable_but_not_current_annual_coverage() -> None:
    from datetime import datetime, timezone

    from db.sgs_refresh_plan import CoverageReceipt, _receipt_scopes

    def proof(code: int, version: str) -> CoverageReceipt:
        return CoverageReceipt(
            operation_id=f"op-{code}-{version}", run_id="run", unit_id="unit",
            source="bcb_sgs", series_code=code, contract_id=f"sgs:{code}",
            contract_version=version, identity_kind="point", scope_role="archive_backfill",
            policy_version="sgs-coverage-policy-v2", requested_start=date(2020, 1, 1),
            requested_end=date(2020, 12, 31), payload_sha256="a" * 64,
            observed_count=2, captured_at=datetime.now(timezone.utc),
            reference_start=date(2020, 1, 1), reference_end=date(2020, 1, 1),
            business_receipt_id="receipt",
        )

    legacy_pib = proof(1207, "acquisition-contract-v1")
    current_pib = proof(1207, "acquisition-contract-v2")
    legacy_other = proof(432, "acquisition-contract-v1")
    assert (1207, date(2020, 1, 1), date(2020, 12, 31)) not in _receipt_scopes((legacy_pib,))
    scopes = _receipt_scopes((current_pib, legacy_other))
    assert (1207, date(2020, 1, 1), date(2020, 12, 31)) in scopes
    assert scopes[(1207, date(2020, 1, 1), date(2020, 12, 31))] == current_pib
    assert (432, date(2020, 1, 1), date(2020, 12, 31)) in scopes


@pytest.mark.parametrize(
    "changes",
    (
        {"unit": "brl_million"},
        {"frequency": "quarterly"},
        {"contract_id": "sgs:432"},
        {"identity_kind": "full_period"},
        {"method": "full_period"},
    ),
)
def test_incompatible_1207_pending_intent_is_not_carried_into_new_plan(changes) -> None:
    from dataclasses import replace

    old = build_sgs_plan(
        date(2026, 10, 27), max_archive_units=1, last_archive_series_code=1206,
    )
    canonical = next(unit for unit in old.units if unit.series_code == 1207 and unit.role != "recent")
    malformed = replace(canonical, **changes)

    next_plan = build_sgs_plan(
        date(2026, 10, 28), max_archive_units=1, carryover_units=(malformed,),
    )

    assert all(
        (unit.contract_version, unit.unit, unit.frequency, unit.contract_id,
         unit.identity_kind, unit.method)
        == ("acquisition-contract-v2", "brl", "annual", "sgs:1207", "point", "date_range")
        for unit in next_plan.units if unit.series_code == 1207
    )


def test_canonical_pending_intent_carries_exactly_and_preserves_other_series_contracts() -> None:
    old = build_sgs_plan(
        date(2026, 10, 27), max_archive_units=1, last_archive_series_code=1206,
    )
    pib_archive = next(unit for unit in old.units if unit.series_code == 1207 and unit.role != "recent")
    carried = build_sgs_plan(
        date(2026, 10, 28), max_archive_units=1, carryover_units=(pib_archive,),
    )
    assert carried.units[-1] == pib_archive

    other = build_sgs_plan(
        date(2026, 10, 27), max_archive_units=1, last_archive_series_code=431,
    )
    other_archive = next(unit for unit in other.units if unit.series_code == 432 and unit.role != "recent")
    other_next = build_sgs_plan(
        date(2026, 10, 28), max_archive_units=1, carryover_units=(other_archive,),
    )
    assert other_next.units[-1] == other_archive
    assert other_archive.contract_version == "acquisition-contract-v1"

    tr_plan = build_sgs_plan(
        date(2026, 10, 27), max_archive_units=1, last_archive_series_code=225,
    )
    tr_archive = next(unit for unit in tr_plan.units if unit.series_code == 226 and unit.role != "recent")
    tr_next = build_sgs_plan(
        date(2026, 10, 28), max_archive_units=1, carryover_units=(tr_archive,),
    )
    assert tr_next.units[-1] == tr_archive
    assert (tr_archive.unit, tr_archive.frequency, tr_archive.identity_kind, tr_archive.method) == (
        "pct_period", "monthly", "full_period", "full_period"
    )


def test_corrupt_persisted_1207_archive_intent_is_not_rewritten_or_frozen_forward() -> None:
    old = build_sgs_plan(
        date(2026, 10, 27), max_archive_units=1, last_archive_series_code=1206,
    )
    archive = next(unit for unit in old.units if unit.series_code == 1207 and unit.role != "recent")
    freeze_sgs_plan("sgs-corrupt-pending-old", old)
    assert set_archive_unit_state(
        "sgs-corrupt-pending-old", archive.unit_id, "deferred", "LegacyPending",
    )
    store.execute_statement(
        "UPDATE sgs_refresh_plan_units SET unit = 'brl_million', frequency = 'quarterly' "
        "WHERE run_id = ? AND unit_id = ?",
        ("sgs-corrupt-pending-old", archive.unit_id),
    )
    old_hash = store.query_rows(
        "SELECT plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?",
        ("sgs-corrupt-pending-old",),
    )[0]["plan_sha256"]

    planned = sgs_refresh_plan.build_next_sgs_plan(date(2026, 10, 28), max_archive_units=1)
    freeze_sgs_plan("sgs-corrupt-pending-new", planned)

    old_row = store.query_rows(
        "SELECT unit, frequency, contract_version, status FROM sgs_refresh_plan_units "
        "WHERE run_id = ? AND unit_id = ?",
        ("sgs-corrupt-pending-old", archive.unit_id),
    )[0]
    assert old_row == {
        "unit": "brl_million", "frequency": "quarterly",
        "contract_version": "acquisition-contract-v2", "status": "deferred",
    }
    assert store.query_rows(
        "SELECT plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?",
        ("sgs-corrupt-pending-old",),
    )[0]["plan_sha256"] == old_hash
    assert all(
        (unit.contract_version, unit.unit, unit.frequency, unit.contract_id,
         unit.identity_kind, unit.method)
        == ("acquisition-contract-v2", "brl", "annual", "sgs:1207", "point", "date_range")
        for unit in planned.units if unit.series_code == 1207
    )
    assert all(
        not (unit.unit == "brl_million" and unit.frequency == "quarterly")
        for unit in planned.units
    )


def test_known_sgs_1207_annual_values_are_hashed_without_unit_conversion() -> None:
    from db.sgs_refresh_plan import canonical_sgs_values

    records = [
        {"data": "01/01/2024", "valor": "11779250581628.30"},
        {"data": "01/01/2025", "valor": "12738565613881.10"},
    ]
    digest, count, start, end = canonical_sgs_values(1207, records)
    assert len(digest) == 64
    assert count == 2
    assert (start, end) == (date(2024, 1, 1), date(2025, 1, 1))
    assert [float(row["valor"]) for row in records] == [
        11779250581628.30, 12738565613881.10
    ]


def test_frozen_v1_plan_roundtrips_without_policy_rewrite() -> None:
    import json

    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=0)
    legacy_units = tuple(replace(unit, policy_version="sgs-coverage-policy-v1") for unit in plan.units)
    legacy = replace(plan, policy_version="sgs-coverage-policy-v1", units=legacy_units)
    frozen = json.dumps(sgs_refresh_plan._plan_json(legacy), sort_keys=True, separators=(",", ":"))
    decoded = sgs_refresh_plan._plan_from_json(json.loads(frozen))
    assert decoded == legacy
    assert json.dumps(sgs_refresh_plan._plan_json(decoded), sort_keys=True, separators=(",", ":")) == frozen
    assert sgs_refresh_plan.plan_digest(decoded) == __import__("hashlib").sha256(frozen.encode()).hexdigest()
    unknown = json.loads(frozen)
    unknown["policy_version"] = "sgs-coverage-policy-v99"
    with pytest.raises(sgs_refresh_plan.UnsupportedSGSPlanError, match="unsupported"):
        sgs_refresh_plan._plan_from_json(unknown)
    for malformed_policy in ([], {}):
        malformed = json.loads(frozen)
        malformed["policy_version"] = malformed_policy
        with pytest.raises(sgs_refresh_plan.UnsupportedSGSPlanError):
            sgs_refresh_plan._plan_from_json(malformed)
    inconsistent = json.loads(frozen)
    inconsistent["policy_version"] = sgs_refresh_plan.POLICY_VERSION
    with pytest.raises(sgs_refresh_plan.UnsupportedSGSPlanError, match="recent"):
        sgs_refresh_plan._plan_from_json(inconsistent)


def test_v2_plan_can_carry_exact_v1_archive_intent() -> None:
    from db.sgs_refresh_plan import SGSPlan

    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=1)
    archive = next(unit for unit in plan.units if unit.role != "recent")
    carried = replace(archive, policy_version="sgs-coverage-policy-v1")
    mixed = SGSPlan(plan.plan_version, plan.policy_version, plan.as_of,
                    tuple(carried if unit is archive else unit for unit in plan.units))
    encoded = sgs_refresh_plan._plan_json(mixed)
    assert sgs_refresh_plan._plan_from_json(encoded) == mixed


def test_recent_scope_respects_approved_history_start() -> None:
    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=0)
    by_code = {unit.series_code: unit for unit in plan.units}
    assert by_code[226].start_date == date(2026, 7, 29)
    assert by_code[432].start_date == date(2026, 7, 29)
    assert min(unit.start_date for unit in plan.units) >= date(1990, 1, 1)
    for group, expected in (
        (sgs_refresh_plan.CatalogGroup.FISCAL, date(2001, 1, 1)),
        (sgs_refresh_plan.CatalogGroup.CREDIT, date(2011, 1, 1)),
        (sgs_refresh_plan.CatalogGroup.INTEREST_RATES, date(2015, 1, 1)),
        (sgs_refresh_plan.CatalogGroup.IPCA_GROUPS, date(1990, 1, 1)),
    ):
        spec = next(spec for spec in sgs_refresh_plan.INDICATOR_CATALOG.values() if spec.group is group)
        windows = sgs_refresh_plan._archive_windows(
            expected,
            sgs_refresh_plan._recent_start(spec, date(2026, 10, 27)),
        )
        assert windows[0][0] == expected


def test_calendar_helpers_cover_december_leap_year_and_slash_dates() -> None:
    assert sgs_refresh_plan._days_in_month(2024, 12) == 31
    assert sgs_refresh_plan._days_in_month(2024, 2) == 29
    assert sgs_refresh_plan._month_shift(date(2024, 2, 29), 12) == date(2023, 2, 28)
    windows = sgs_refresh_plan._archive_windows(date(2020, 2, 29), date(2022, 3, 1))
    assert windows[0] == (date(2020, 2, 29), date(2021, 2, 27))
    assert all((end - start).days <= 365 for start, end in windows)


def test_unknown_catalog_policy_and_duplicate_series_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = next(iter(sgs_refresh_plan.INDICATOR_CATALOG.values()))
    monkeypatch.delitem(sgs_refresh_plan._LOOKBACK, spec.frequency)
    with pytest.raises(sgs_refresh_plan.UnsupportedSGSPlanError, match="unsupported"):
        build_sgs_plan(date(2026, 10, 27))
    monkeypatch.undo()
    spec = next(iter(sgs_refresh_plan.INDICATOR_CATALOG.values()))
    monkeypatch.delitem(sgs_refresh_plan._APPROVED_GROUP_START, spec.group)
    with pytest.raises(sgs_refresh_plan.UnsupportedSGSPlanError, match="unsupported"):
        build_sgs_plan(date(2026, 10, 27))

    monkeypatch.undo()
    catalog = dict(sgs_refresh_plan.INDICATOR_CATALOG)
    first = next(iter(catalog.values()))
    from indicator_catalog import IndicatorKey

    catalog[IndicatorKey("duplicate-key")] = replace(first, key=IndicatorKey("duplicate-key"))
    monkeypatch.setattr(sgs_refresh_plan, "INDICATOR_CATALOG", catalog)
    with pytest.raises(sgs_refresh_plan.UnsupportedSGSPlanError, match="not unique"):
        build_sgs_plan(date(2026, 10, 27))


def test_archive_selection_caps_and_rotates_series_deterministically() -> None:
    first = build_sgs_plan(date(2026, 10, 27))
    first_archives = [unit.series_code for unit in first.units if unit.role != "recent"]
    cursor = max(first_archives)
    next_plan = build_sgs_plan(date(2026, 10, 27), last_archive_series_code=cursor)
    next_archives = [unit.series_code for unit in next_plan.units if unit.role != "recent"]
    assert len(set(first_archives)) == 4
    assert len(set(next_archives)) == 4
    assert first_archives != next_archives
    next_day = build_sgs_plan(date(2026, 10, 28))
    assert [unit.unit_id for unit in first.units if unit.role != "recent"] == [
        unit.unit_id for unit in next_day.units if unit.role != "recent"
    ]
    with pytest.raises(ValueError, match="policy cap"):
        build_sgs_plan(date(2026, 10, 27), max_archive_units=5)


def test_receipt_scope_ignores_unrecognized_and_nonarchive_evidence() -> None:
    from dataclasses import replace

    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=1)
    archive = next(unit for unit in plan.units if unit.role != "recent")
    valid_shape = CoverageReceipt(
        operation_id="ignored", run_id="old", unit_id="old", source="bcb_sgs",
        series_code=archive.series_code, contract_id=archive.contract_id,
        contract_version="acquisition-contract-v1", identity_kind=archive.identity_kind,
        scope_role="archive_backfill", policy_version=archive.policy_version,
        requested_start=archive.start_date, requested_end=archive.end_date,
        payload_sha256="0" * 64, observed_count=1,
        captured_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
        reference_start=None, reference_end=None, business_receipt_id="unused",
    )
    evidence = (
        replace(valid_shape, source="other"),
        replace(valid_shape, contract_version="old-contract"),
        replace(valid_shape, policy_version="old-policy"),
        replace(valid_shape, scope_role="recent"),
    )
    assert sgs_refresh_plan._receipt_scopes(evidence) == {}
    legacy_proof = replace(valid_shape, policy_version="sgs-coverage-policy-v1")
    assert sgs_refresh_plan._receipt_scopes((legacy_proof,))[
        (archive.series_code, archive.start_date, archive.end_date)
    ] == legacy_proof


def test_receipt_not_date_bounds_or_counts_drives_archive_priority() -> None:
    first = build_sgs_plan(date(2026, 10, 27), max_archive_units=1)
    chosen = next(unit for unit in first.units if unit.role != "recent")
    fake = CoverageReceipt(
        operation_id="fake", run_id="old", unit_id="old-unit", source="bcb_sgs",
        series_code=chosen.series_code, contract_id=f"sgs:{chosen.series_code}",
        contract_version="unknown-v99", identity_kind=chosen.identity_kind,
        scope_role="archive_backfill", policy_version=chosen.policy_version,
        requested_start=chosen.start_date, requested_end=chosen.end_date,
        payload_sha256="0" * 64, observed_count=100000,
        captured_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
        reference_start=chosen.start_date, reference_end=chosen.end_date,
        business_receipt_id="unknown-business",
    )
    without = build_sgs_plan(date(2026, 10, 27), receipts=(fake,), max_archive_units=1)
    assert without.units[-1].unit_id == chosen.unit_id


def _known_business_receipt(operation_id: str, series_code: int, run_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    store.start_source_extraction({
        "extraction_id": operation_id, "source": "bcb_sgs", "indicator": str(series_code),
        "qualifiers_json": "{}", "status": "running", "network_called": 1,
        "started_at": now, "run_id": run_id,
    })
    store.finish_source_extraction({
        "extraction_id": operation_id, "status": "success", "network_called": 1,
        "finished_at": now, "extracted_at": now, "source_data_date": None,
        "source_reference_min": "2015-01-01", "source_reference_max": "2015-12-31",
        "received_count": 1, "valid_count": 1, "persisted_count": 1,
        "inserted_count": 0, "updated_count": 1, "request_count": 1, "retry_count": 0,
        "error_code": None, "error_detail": None, "error_category": None,
        "retry_not_before": None,
    })


def test_freeze_is_idempotent_and_receipt_requires_verified_business_commit() -> None:
    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=1)
    run_id = "sgs-plan-freeze"
    frozen = freeze_sgs_plan(run_id, plan)
    changed_runtime_plan = build_sgs_plan(date(2026, 10, 28), max_archive_units=0)
    resumed = freeze_sgs_plan(run_id, changed_runtime_plan)
    assert resumed == frozen
    archive = next(unit for unit in frozen.units if unit.role != "recent")
    proof = CoverageReceipt(
        operation_id="sgs-op-1", run_id=run_id, unit_id=archive.unit_id, source="bcb_sgs",
        series_code=archive.series_code, contract_id=archive.contract_id,
        contract_version=archive.contract_version, identity_kind=archive.identity_kind,
        scope_role=archive.role, policy_version=archive.policy_version,
        requested_start=archive.start_date, requested_end=archive.end_date,
        payload_sha256="a" * 64, observed_count=1,
        captured_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
        reference_start=archive.start_date, reference_end=archive.end_date,
        business_receipt_id="not-known",
    )
    with pytest.raises(ValueError, match="exact durable SGS unit-commit receipt"):
        record_verified_coverage(proof)
    assert verified_coverage(archive.series_code) == ()
    assert store._query(
        "SELECT status FROM sgs_refresh_plan_units WHERE run_id = ? AND unit_id = ?",
        (run_id, archive.unit_id),
    )[0]["status"] == "reserved"


def test_receipt_verification_is_idempotent_and_conflicting_payload_rejected() -> None:
    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=1)
    run_id = "sgs-plan-receipt"
    freeze_sgs_plan(run_id, plan)
    archive = next(unit for unit in plan.units if unit.role != "recent")
    historical = (archive.series_code, archive.start_date.isoformat(), 9.5, "2015-01-01T00:00:00+00:00")
    store._execute(
        "INSERT INTO sgs (series_code, date, value, fetched_at) VALUES (?, ?, ?, ?)", historical
    )
    value_rows = [{"data": archive.start_date.strftime("%d/%m/%Y"), "valor": 9.5}]
    canonical_hash, count, ref_start, ref_end = sgs_refresh_plan.canonical_sgs_values(
        archive.series_code, value_rows
    )
    business_commit = SGSUnitCommitReceipt(
        operation_id="business-receipt-1", extraction_id="business-receipt-1",
        run_id=run_id, unit_id=archive.unit_id,
        plan_sha256=sgs_refresh_plan.plan_digest(plan), series_code=archive.series_code,
        contract_id=archive.contract_id, contract_version=archive.contract_version,
        identity_kind=archive.identity_kind, scope_role=archive.role,
        policy_version=archive.policy_version, requested_start=archive.start_date,
        requested_end=archive.end_date, payload_sha256="b" * 64,
        canonical_value_sha256=canonical_hash, observed_count=count,
        reference_start=ref_start, reference_end=ref_end,
        captured_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
    )
    store.upsert_sgs(archive.series_code, value_rows, sgs_unit_commit_receipt=business_commit)
    proof = CoverageReceipt(
        operation_id="business-receipt-1", run_id=run_id, unit_id=archive.unit_id,
        source="bcb_sgs", series_code=archive.series_code, contract_id=archive.contract_id,
        contract_version=archive.contract_version, identity_kind=archive.identity_kind,
        scope_role=archive.role, policy_version=archive.policy_version,
        requested_start=archive.start_date, requested_end=archive.end_date,
        payload_sha256="b" * 64, observed_count=0,
        captured_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
        reference_start=None, reference_end=None, business_receipt_id="business-receipt-1",
    )
    with pytest.raises(ValueError, match="incomplete or invalid"):
        record_verified_coverage(proof)
    valid_proof = replace(
        proof, observed_count=count, reference_start=ref_start, reference_end=ref_end,
        captured_at=business_commit.captured_at,
    )
    with pytest.raises(ValueError, match="exact durable SGS unit-commit receipt"):
        record_verified_coverage(replace(valid_proof, unit_id="not-frozen"))
    assert record_verified_coverage(valid_proof) is True
    assert record_verified_coverage(valid_proof) is False
    assert len(verified_coverage(archive.series_code)) == 1
    assert store._query(
        "SELECT series_code, date, value, fetched_at FROM sgs WHERE series_code = ? AND date = ?",
        historical[:2],
    )[0] == {
        "series_code": historical[0], "date": historical[1],
        "value": historical[2], "fetched_at": historical[3],
    }
    assert store._query(
        "SELECT last_series_code FROM sgs_archive_cursors WHERE source = 'bcb_sgs'"
    )[0]["last_series_code"] == archive.series_code
    with pytest.raises(ValueError, match="exact durable SGS unit-commit receipt"):
        record_verified_coverage(replace(valid_proof, payload_sha256="c" * 64))
    with pytest.raises(ValueError, match="exact durable SGS unit-commit receipt"):
        record_verified_coverage(replace(valid_proof, observed_count=2))


def test_atomic_unit_commit_retains_first_seen_and_appends_primary_revision() -> None:
    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=0)
    freeze_sgs_plan("sgs-vintage-commit", plan)
    unit = plan.units[0]
    capture1 = datetime(2026, 10, 27, tzinfo=timezone.utc)

    def commit_for(op_id: str, value: float, captured_at: datetime) -> SGSUnitCommitReceipt:
        rows = [{"data": unit.start_date.strftime("%d/%m/%Y"), "valor": value}]
        value_hash, count, reference_start, reference_end = sgs_refresh_plan.canonical_sgs_values(
            unit.series_code, rows
        )
        return SGSUnitCommitReceipt(
            operation_id=op_id, extraction_id=op_id, run_id="sgs-vintage-commit",
            unit_id=unit.unit_id, plan_sha256=sgs_refresh_plan.plan_digest(plan),
            series_code=unit.series_code, contract_id=unit.contract_id,
            contract_version=unit.contract_version, identity_kind=unit.identity_kind,
            scope_role=unit.role, policy_version=unit.policy_version,
            requested_start=unit.start_date, requested_end=unit.end_date,
            payload_sha256=("a" if value == 1.5 else "b") * 64,
            canonical_value_sha256=value_hash, observed_count=count,
            reference_start=reference_start, reference_end=reference_end,
            captured_at=captured_at,
        )

    row = {"data": unit.start_date.strftime("%d/%m/%Y"), "valor": 1.5}
    store.upsert_sgs(unit.series_code, [row], first_seen_at=capture1.isoformat(),
                     sgs_unit_commit_receipt=commit_for("sgs-vintage-1", 1.5, capture1))
    store.upsert_sgs(unit.series_code, [row], first_seen_at="2026-10-28T00:00:00+00:00",
                     sgs_unit_commit_receipt=commit_for(
                         "sgs-vintage-2", 1.5, datetime(2026, 10, 28, tzinfo=timezone.utc)
                     ))
    assert store._query(
        "SELECT COUNT(*) AS count, MIN(first_seen_at) AS first_seen FROM observation_vintages "
        "WHERE provider = 'bcb_sgs' AND series_key = ? AND reference_period = ?",
        (unit.indicator_key, unit.start_date.isoformat()),
    )[0] == {"count": 1, "first_seen": capture1.isoformat()}
    revised = {"data": unit.start_date.strftime("%d/%m/%Y"), "valor": 2.5}
    store.upsert_sgs(unit.series_code, [revised], first_seen_at="2026-10-29T00:00:00+00:00",
                     sgs_unit_commit_receipt=commit_for(
                         "sgs-vintage-3", 2.5, datetime(2026, 10, 29, tzinfo=timezone.utc)
                     ))
    assert store._query(
        "SELECT COUNT(*) AS count FROM observation_vintages WHERE provider = 'bcb_sgs' "
        "AND series_key = ? AND reference_period = ?",
        (unit.indicator_key, unit.start_date.isoformat()),
    )[0]["count"] == 2


def test_unit_commit_rejects_frozen_plan_identity_mismatches() -> None:
    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=0)
    freeze_sgs_plan("sgs-unit-mismatch", plan)
    unit = plan.units[0]
    rows = [{"data": unit.start_date.strftime("%d/%m/%Y"), "valor": 1.5}]
    value_hash, count, reference_start, reference_end = sgs_refresh_plan.canonical_sgs_values(
        unit.series_code, rows
    )
    valid = SGSUnitCommitReceipt(
        operation_id="mismatch-op", extraction_id="mismatch-op", run_id="sgs-unit-mismatch",
        unit_id=unit.unit_id, plan_sha256=sgs_refresh_plan.plan_digest(plan),
        series_code=unit.series_code, contract_id=unit.contract_id,
        contract_version=unit.contract_version, identity_kind=unit.identity_kind,
        scope_role=unit.role, policy_version=unit.policy_version,
        requested_start=unit.start_date, requested_end=unit.end_date,
        payload_sha256="a" * 64, canonical_value_sha256=value_hash, observed_count=count,
        reference_start=reference_start, reference_end=reference_end,
        captured_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
    )
    for mismatch in (
        replace(valid, plan_sha256="b" * 64),
        replace(valid, scope_role="archive_backfill"),
        replace(valid, identity_kind="full_period"),
        replace(valid, unit_id="wrong-unit"),
        replace(valid, observed_count=2),
    ):
        with pytest.raises(ValueError, match="frozen plan|business rows"):
            store.upsert_sgs(unit.series_code, rows, sgs_unit_commit_receipt=mismatch)
    assert store._query("SELECT * FROM sgs_unit_commits") == []


def test_tr_unit_commit_preserves_full_interval_and_allows_future_period_end() -> None:
    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=0)
    freeze_sgs_plan("sgs-tr-unit", plan)
    unit = next(item for item in plan.units if item.series_code == 226)
    start = unit.start_date
    future_end = unit.end_date.replace(year=unit.end_date.year + 1)
    source_rows = [{
        "data": start.strftime("%d/%m/%Y"),
        "dataFim": future_end.strftime("%d/%m/%Y"),
        "valor": "0.0032",
    }]
    value_hash, count, reference_start, reference_end = sgs_refresh_plan.canonical_sgs_values(
        226, source_rows
    )
    receipt = SGSUnitCommitReceipt(
        operation_id="sgs-tr-future", extraction_id="sgs-tr-future", run_id="sgs-tr-unit",
        unit_id=unit.unit_id, plan_sha256=sgs_refresh_plan.plan_digest(plan),
        series_code=226, contract_id=unit.contract_id,
        contract_version=unit.contract_version, identity_kind="full_period",
        scope_role=unit.role, policy_version=unit.policy_version,
        requested_start=unit.start_date, requested_end=unit.end_date,
        payload_sha256="f" * 64, canonical_value_sha256=value_hash, observed_count=count,
        reference_start=reference_start, reference_end=reference_end,
        captured_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
    )
    store.upsert_tr_periods(source_rows, sgs_unit_commit_receipt=receipt)
    stored = store._query(
        "SELECT period_start, period_end FROM tr_period_observations WHERE period_start = ?",
        (start.isoformat(),),
    )
    assert stored == [{"period_start": start.isoformat(), "period_end": future_end.isoformat()}]


def test_deferred_archive_intent_carries_forward_without_verifying_coverage() -> None:
    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=1)
    freeze_sgs_plan("sgs-plan-carry", plan)
    archive = next(unit for unit in plan.units if unit.role != "recent")
    assert set_archive_unit_state("sgs-plan-carry", archive.unit_id, "deferred", "RetryAfter")
    pending = pending_archive_units()
    assert pending == (archive,)
    next_plan = build_sgs_plan(
        date(2026, 10, 28), max_archive_units=1, carryover_units=pending,
    )
    assert next_plan.units[-1].unit_id == archive.unit_id
    assert verified_coverage(archive.series_code) == ()
