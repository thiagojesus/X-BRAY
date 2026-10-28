from datetime import datetime, timezone

from refresh_reporting import _v6_group_reference_projection


def _verified_member(code: int, reference: str, frequency: str, freshness: int) -> dict[str, object]:
    return {
        "series_code": code,
        "eligible_reference_max": reference,
        "reference_end": reference,
        "frequency": frequency,
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
