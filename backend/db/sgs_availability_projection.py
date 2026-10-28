"""Read-only public projection of frozen SGS availability metadata."""

from __future__ import annotations

from datetime import date, timedelta

from db import store
from db.sgs_refresh_plan import (
    POLICY_VERSION,
    SGSPlan,
    SGSPlanUnit,
    UnsupportedSGSPlanError,
)


def _frozen_recent_units(plan: SGSPlan) -> tuple[SGSPlanUnit, ...]:
    recent = tuple(unit for unit in plan.units if unit.role == "recent")
    if not recent:
        raise UnsupportedSGSPlanError("frozen SGS availability snapshot has no recent units")
    for unit in recent:
        if (
            unit.availability_start is None or unit.availability_documented_start is None
            or unit.approved_history_start is None or unit.availability_observed_on is None
            or not unit.availability_registry_version or not unit.availability_registry_sha256
            or not unit.availability_source_url or not unit.availability_detail_url
        ):
            raise UnsupportedSGSPlanError("frozen SGS availability facts are incomplete")
    first = recent[0]
    assert first.availability_registry_version is not None
    assert first.availability_registry_sha256 is not None
    assert first.availability_observed_on is not None
    if any(
        (unit.availability_registry_version, unit.availability_registry_sha256,
         unit.availability_observed_on)
        != (first.availability_registry_version, first.availability_registry_sha256,
            first.availability_observed_on)
        for unit in recent
    ):
        raise UnsupportedSGSPlanError("frozen SGS availability registry snapshot is inconsistent")
    return recent


def availability_projection(plan: SGSPlan) -> dict[str, object]:
    """Report boundaries and exact-proof conflicts, never coverage success."""
    if plan.policy_version != POLICY_VERSION:
        return {
            "status": "not_recorded", "registry_version": None, "registry_sha256": None,
            "observed_on": None, "basis": None, "by_series": [],
            "excluded_scopes": [], "conflicts": [],
        }
    recent = _frozen_recent_units(plan)
    by_code = {unit.series_code: unit for unit in recent}
    first = recent[0]
    assert first.availability_registry_version is not None
    assert first.availability_registry_sha256 is not None
    assert first.availability_observed_on is not None
    proof_starts = store.sgs_preavailability_reference_min(tuple(
        (unit.series_code, unit.contract_version, unit.identity_kind)
        for unit in by_code.values()
    ))
    series_facts: list[dict[str, object]] = []
    exclusions: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []
    for code, unit in sorted(by_code.items()):
        first_period = unit.availability_start
        documented = unit.availability_documented_start
        approved = unit.approved_history_start
        assert first_period is not None and documented is not None and approved is not None
        assert unit.availability_observed_on is not None
        assert unit.availability_basis is not None
        assert unit.availability_source_url is not None and unit.availability_detail_url is not None
        status = "documented_boundary"
        proof_start = proof_starts.get(code)
        if proof_start is not None:
            try:
                proof_date = date.fromisoformat(proof_start)
            except ValueError as exc:
                raise UnsupportedSGSPlanError("persisted SGS proof reference date is invalid") from exc
            if proof_date < first_period:
                status = "conflict_requires_review"
                conflicts.append({
                    "series_code": code, "earliest_proven_reference": proof_date.isoformat(),
                    "documented_first_reference_period": first_period.isoformat(),
                    "disposition": "conservative_unknown_metadata_review",
                })
        series_facts.append({
            "series_code": code, "documented_start": documented.isoformat(),
            "first_reference_period": first_period.isoformat(),
            "approved_history_start": approved.isoformat(), "status": status,
            "basis": unit.availability_basis,
            "observed_on": unit.availability_observed_on.isoformat(),
            "source_urls": [unit.availability_source_url, unit.availability_detail_url],
        })
        if first_period > approved:
            exclusions.append({
                "series_code": code, "start_date": approved.isoformat(),
                "end_date": (first_period - timedelta(days=1)).isoformat(),
                "reason": "before_documented_start", "status": "excluded_from_required_scope",
                "basis": unit.availability_basis,
            })
    result: dict[str, object] = {
        "status": "conflict_requires_review" if conflicts else "documented_boundary_only",
        "registry_version": first.availability_registry_version,
        "registry_sha256": first.availability_registry_sha256,
        "observed_on": first.availability_observed_on.isoformat(),
        "basis": "documented_start_not_coverage_proof", "by_series": series_facts,
        "excluded_scopes": exclusions, "conflicts": conflicts,
    }
    return result
