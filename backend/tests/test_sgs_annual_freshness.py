from datetime import datetime, timezone

from refresh_reporting import _v6_group_reference_projection


def _verified_member(code: int, reference: str, frequency: str, freshness: int) -> dict[str, object]:
    return {
        "series_code": code,
        "identity_kind": "full_period" if code == 226 else "point",
        "eligible_reference_max": reference,
        "reference_end": reference,
        "frequency": frequency,
        "method": "date_range",
        "freshness_days": freshness,
        "commit_operation_id": "op",
        "commit_run_id": "run",
        "commit_payload_sha256": "a" * 64,
        "canonical_value_sha256": "b" * 64,
        "commit_observed_count": 2,
        "data_captured_at": "2026-10-28T10:00:00+00:00",
    }


def test_annual_reference_freshness_uses_year_end_but_preserves_actual_reference_date() -> None:
    annual = _verified_member(1207, "2025-01-01", "annual", 400)
    projection = _v6_group_reference_projection(
        [annual], datetime(2026, 10, 28, tzinfo=timezone.utc)
    )
    assert projection["current_reference_max"] == "2025-01-01"
    assert projection["reference_freshness_status"] == "ok"
    assert projection["series_reference_freshness"] == {"1207": "ok"}


def test_old_annual_reference_and_other_series_staleness_are_not_hidden() -> None:
    annual = _verified_member(1207, "2023-01-01", "annual", 400)
    monthly = _verified_member(433, "2026-01-01", "monthly", 75)
    projection = _v6_group_reference_projection(
        [annual, monthly], datetime(2026, 10, 28, tzinfo=timezone.utc)
    )
    assert projection["series_reference_freshness"] == {"1207": "stale", "433": "stale"}
    assert projection["reference_freshness_status"] == "stale"


def test_monthly_reference_uses_period_end_without_relabeling_observation() -> None:
    monthly = _verified_member(4639, "2026-08-01", "monthly", 75)
    projection = _v6_group_reference_projection(
        [monthly], datetime(2026, 10, 28, tzinfo=timezone.utc)
    )
    assert projection["current_reference_max"] == "2026-08-01"
    assert projection["reference_freshness_status"] == "ok"
    detail = projection["series_reference_freshness_details"]["4639"]
    assert detail["basis_date"] == "2026-08-31"
    assert detail["deadline"] == "2026-11-14"
    assert detail["policy_version"] == "reference-freshness-v2"


def test_tr_full_period_uses_eligible_start_not_future_period_end() -> None:
    tr = _verified_member(226, "2026-08-01", "monthly", 75)
    tr["method"] = "full_period"
    tr["eligible_period_end_max"] = "2026-11-26"
    projection = _v6_group_reference_projection(
        [tr], datetime(2026, 10, 28, tzinfo=timezone.utc)
    )
    assert projection["current_reference_max"] == "2026-08-01"
    assert projection["series_reference_freshness"] == {"226": "stale"}
    detail = projection["series_reference_freshness_details"]["226"]
    assert detail["basis_date"] == "2026-08-01"
    assert detail["deadline"] == "2026-10-15"
    assert projection["series_period_end_max"] == {"226": "2026-11-26"}


def test_future_observation_reference_is_unverified_and_not_reported_as_current_max() -> None:
    future = _verified_member(4639, "2026-11-01", "monthly", 75)
    projection = _v6_group_reference_projection(
        [future], datetime(2026, 10, 28, tzinfo=timezone.utc)
    )
    assert projection["current_reference_max"] is None
    assert projection["reference_freshness_status"] == "unverified"
    assert projection["series_reference_freshness"] == {"4639": "unverified"}


def test_known_august_sgs_matrix_clears_only_the_17_seventy_five_day_warnings() -> None:
    accepted_codes = (
        4639, 24363, 24364, 24382, 29602, 29604, 29606, 29608,
        4513, 5727, 5760, 5793, 13762, 1788, 1835, 27788, 27810,
    )
    remaining_credit_codes = (20716, 21084, 25353)
    july_codes = (29034, 29037)
    members = [
        _verified_member(code, "2026-08-01", "monthly", 75)
        for code in accepted_codes
    ]
    members.extend(
        _verified_member(code, "2026-08-01", "monthly", 45)
        for code in remaining_credit_codes
    )
    members.extend(
        _verified_member(code, "2026-07-01", "monthly", 75)
        for code in july_codes
    )

    projection = _v6_group_reference_projection(
        members, datetime(2026, 10, 28, 17, 54, 12, tzinfo=timezone.utc)
    )

    statuses = projection["series_reference_freshness"]
    assert len(statuses) == 22
    assert all(statuses[str(code)] == "ok" for code in accepted_codes)
    assert all(statuses[str(code)] == "stale" for code in (*remaining_credit_codes, *july_codes))
    details = projection["series_reference_freshness_details"]
    assert details["4639"]["observed_reference"] == "2026-08-01"
    assert details["4639"]["basis_date"] == "2026-08-31"
    assert details["20716"]["deadline"] == "2026-10-15"
    assert details["29034"]["basis_date"] == "2026-07-31"
    assert details["29034"]["deadline"] == "2026-10-14"
    assert projection["current_reference_max"] == "2026-08-01"
    assert projection["reference_freshness_status"] == "stale"
