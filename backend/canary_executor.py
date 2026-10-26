"""Commit one approved paired canary unit with immutable receipt/provenance."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import cast

from canary_acquisition import (
    CanaryAcquisitionError,
    CanaryComparison,
    CanaryPlan,
    acquire_sgs_canary,
    acquire_sidra_canary,
    build_sgs_plan,
    build_sidra_plan,
    request_plan_sha256,
)
from candidate_acquisition import (
    CandidateAcquisitionService,
    build_origin_rows,
    commit_receipt,
)
from candidate_eligibility import CandidateApproval, profile_semantic_sha256
from db import store
from domain_contracts import Record
from ingestion_policy import current_retry_budget
from source_candidates import CandidateProfile, ShadowAdapter
from source_ingestion import (
    REGISTRY,
    FetchResult,
    breaker_scope_id,
    source_operation_id,
)


@dataclass(frozen=True, slots=True)
class CanaryCommitOutcome:
    operation_id: str
    candidate_id: str
    reference_period: str | None
    selected_observations: int
    business_writes: int
    inserted_count: int
    updated_count: int
    origin_count: int
    receipt_count: int
    request_count: int
    captured_at: str
    status: str
    committed: bool


@dataclass(frozen=True, slots=True)
class CanaryRequestBinding:
    plan: CanaryPlan
    plan_sha256: str
    profile_semantic_sha256: str


class CanaryExecutionError(RuntimeError):
    """Acquisition, transaction, or audit reporting failed without a known commit."""


def make_request_binding(profile: CandidateProfile, as_of: date) -> CanaryRequestBinding:
    """Derive a stable catalog-owned plan hash before any provider request."""
    if profile.adapter is ShadowAdapter.SGS_CSV:
        plan = build_sgs_plan(profile, as_of)
    elif profile.adapter is ShadowAdapter.SIDRA_V3:
        plan = build_sidra_plan(profile, as_of)
    else:
        raise ValueError("candidate adapter is unsupported for a write canary")
    return CanaryRequestBinding(
        plan,
        request_plan_sha256(plan, profile),
        profile_semantic_sha256(profile),
    )


def binding_matches_request(
    binding: store.SourceCanaryBinding,
    *,
    profile: CandidateProfile,
    approval: CandidateApproval,
    request_binding: CanaryRequestBinding,
    operation_id: str,
    as_of: date,
) -> bool:
    """Compare the persisted immutable request fields before allowing replay."""
    return (
        binding["operation_id"] == operation_id
        and binding["candidate_id"] == profile.candidate_id
        and binding["as_of"] == as_of.isoformat()
        and binding["plan_sha256"] == request_binding.plan_sha256
        and binding["proof_sha256"] == approval.proof_sha256
        and binding["contract_version"] == profile.source_contract_version
        and binding["profile_semantic_sha256"] == request_binding.profile_semantic_sha256
        and binding["parser_semantic_sha256"] == approval.parser_semantic_sha256
    )


def known_receipt_outcome(
    binding: store.SourceCanaryBinding,
    receipt: dict[str, object] | None,
    *,
    status: str = "replayed_known_commit",
) -> CanaryCommitOutcome:
    """Reconstruct durable commit facts without repeating provider requests."""
    if (
        receipt is None
        or receipt.get("extraction_id") != binding["operation_id"]
        or receipt.get("selected_route") != "candidate"
        or receipt.get("candidate_id") != binding["candidate_id"]
    ):
        raise RuntimeError("canary binding does not have its matching durable receipt")
    selected = receipt.get("observation_count")
    inserted = binding["inserted_count"]
    updated = binding["updated_count"]
    if not isinstance(selected, int) or isinstance(selected, bool) or selected < 0:
        raise RuntimeError("canary receipt has invalid observation count")
    return CanaryCommitOutcome(
        binding["operation_id"], binding["candidate_id"],
        binding["actual_reference_period"], selected, inserted + updated,
        inserted, updated, selected, 1, binding["request_count"],
        binding["captured_at"], status, True,
    )


def execute_canary_commit(
    profile: CandidateProfile,
    approval: CandidateApproval,
    as_of: date,
    operation_id: str,
    request_binding: CanaryRequestBinding,
) -> CanaryCommitOutcome:
    """Pair, compare, then atomically persist one unit through the source ledger."""
    if profile.adapter not in {ShadowAdapter.SGS_CSV, ShadowAdapter.SIDRA_V3}:
        raise ValueError("candidate adapter is unsupported for a write canary")
    service = CandidateAcquisitionService(
        enabled=False,
        profiles={profile.candidate_id: profile},
        approvals={profile.candidate_id: approval},
    )
    try:
        with source_operation_id(operation_id):
            result = REGISTRY[profile.source.value].fetch(
                indicator=profile.candidate_id,
                qualifiers={
                    "mode": "canary",
                    "candidate_id": profile.candidate_id,
                    "as_of": as_of.isoformat(),
                    "plan_sha256": request_binding.plan_sha256,
                },
                operation=lambda: _acquire_compare_persist(
                    profile, approval, service, as_of, operation_id, request_binding
                ),
                network_expected=True,
            )
    except Exception as error:
        durable_binding = store.source_canary_binding(operation_id)
        if durable_binding is not None and binding_matches_request(
            durable_binding,
            profile=profile,
            approval=approval,
            request_binding=request_binding,
            operation_id=operation_id,
            as_of=as_of,
        ):
            receipt = store.source_acquisition_commit(operation_id, reconnect=True)
            reconciled_status = (
                "committed_with_reporting_error"
                if getattr(error, "stage", None) == "logging"
                else "reconciled_known_commit"
            )
            outcome = known_receipt_outcome(
                durable_binding,
                receipt,
                status=reconciled_status,
            )
            return replace(
                outcome,
                status=reconciled_status,
                request_count=durable_binding["request_count"],
            )
        raise CanaryExecutionError("canary execution failed without a durable binding") from error
    if not isinstance(result, CanaryCommitOutcome):
        raise TypeError("audited canary returned no commit result")
    return result


def _acquire_compare_persist(
    profile: CandidateProfile,
    approval: CandidateApproval,
    service: CandidateAcquisitionService,
    as_of: date,
    operation_id: str,
    request_binding: CanaryRequestBinding,
) -> FetchResult:
    budget = current_retry_budget()
    if budget is None:
        raise RuntimeError("canary executor requires the audited source retry budget")
    request_start = budget.requests
    comparison = (
        acquire_sgs_canary(profile, approval, service, as_of)
        if profile.adapter is ShadowAdapter.SGS_CSV
        else acquire_sidra_canary(profile, approval, service, as_of)
    )
    candidate_rows = [dict(row) for row in comparison.candidate.observations]
    references = tuple(comparison.candidate.reference_dates)
    request_count = budget.requests - request_start
    if not candidate_rows or not references:
        raise ValueError("paired canary returned an empty candidate cohort")
    actual_period = comparison.plan.reference_period or max(references)
    inserted, updated, changed_rows = _business_changes(profile, candidate_rows)
    origins = _candidate_origins(profile, approval, comparison, operation_id)
    receipt = commit_receipt(
        operation_id,
        route="candidate",
        candidate_id=profile.candidate_id,
        observation_count=len(candidate_rows),
        source_payload_sha256=comparison.candidate.payload_sha256,
    )
    binding = store.CanaryBinding(
        operation_id,
        profile.candidate_id,
        as_of.isoformat(),
        request_binding.plan_sha256,
        approval.proof_sha256,
        profile.source_contract_version,
        request_binding.profile_semantic_sha256,
        approval.parser_semantic_sha256,
        actual_period,
        comparison.captured_at,
        request_count,
        inserted,
        updated,
        datetime.now(timezone.utc).isoformat(),
    )
    _persist_candidate_unit(
        profile, candidate_rows, changed_rows, comparison.captured_at,
        origins, receipt, binding,
    )
    count = len(candidate_rows)
    outcome = CanaryCommitOutcome(
        operation_id,
        profile.candidate_id,
        actual_period,
        count,
        inserted + updated,
        inserted,
        updated,
        len(origins),
        1,
        request_count,
        comparison.captured_at,
        "committed",
        True,
    )
    return FetchResult(
        outcome,
        network_called=True,
        received_count=count,
        valid_count=count,
        persisted_count=inserted + updated,
        inserted_count=inserted,
        updated_count=updated,
        reference_min=min(references),
        reference_max=max(references),
        source_data_date=max(references),
        extracted_at=comparison.captured_at,
        request_count=request_count,
        status="canary_committed",
        clear_primary_breaker=False,
        breaker_scope_id=breaker_scope_id(profile.source.value, "canary", profile.candidate_id),
    )


def _candidate_origins(
    profile: CandidateProfile,
    approval: CandidateApproval,
    comparison: CanaryComparison,
    operation_id: str,
) -> list[tuple[object, ...]]:
    if profile.adapter is ShadowAdapter.SGS_CSV:
        return build_origin_rows(
            extraction_id=operation_id,
            source=profile.source.value,
            contract_id=comparison.plan.contract_id,
            route="candidate",
            candidate_id=profile.candidate_id,
            source_payload_sha256=comparison.candidate.payload_sha256,
            observations=comparison.candidate.observations,
            captured_at=comparison.captured_at,
            contract_version=profile.source_contract_version,
            profile_semantic_sha256=profile_semantic_sha256(profile),
            parser_semantic_sha256=approval.parser_semantic_sha256,
            proof_sha256=approval.proof_sha256,
            identity_fields=("series_code", "reference_date"),
            reference_field="data",
            value_field="valor",
            reference_date=lambda value: datetime.strptime(
                str(value), "%d/%m/%Y"
            ).replace(tzinfo=timezone.utc).date().isoformat(),
        )
    return build_origin_rows(
        extraction_id=operation_id,
        source=profile.source.value,
        contract_id=comparison.plan.contract_id,
        route="candidate",
        candidate_id=profile.candidate_id,
        source_payload_sha256=comparison.candidate.payload_sha256,
        observations=comparison.candidate.observations,
        captured_at=comparison.captured_at,
        contract_version=profile.source_contract_version,
        profile_semantic_sha256=profile_semantic_sha256(profile),
        parser_semantic_sha256=approval.parser_semantic_sha256,
        proof_sha256=approval.proof_sha256,
        identity_fields=(
            "table_id", "variable_id", "territory_level", "territory_code",
            "classification_id", "category_id", "reference_period", "unit", "base_version",
        ),
        reference_field="reference_period",
        value_field="value",
        reference_date=lambda value: _sidra_reference_date(
            str(value), profile.frequency
        ),
    )


def _business_changes(
    profile: CandidateProfile,
    observations: list[dict[str, object]],
) -> tuple[int, int, list[dict[str, object]]]:
    if profile.adapter is ShadowAdapter.SGS_CSV:
        code = int(profile.candidate_id.rsplit(":", 1)[-1])
        from datafetchers.bcb_sgs import query_sgs

        references = [date.fromisoformat(str(item["reference_date"])) for item in observations]
        prior_rows = query_sgs(code, min(references).isoformat(), max(references).isoformat())
        prior = {
            datetime.strptime(str(row["data"]), "%d/%m/%Y").replace(
                tzinfo=timezone.utc
            ).date().isoformat():
            float(str(row["valor"]))
            for row in prior_rows
        }
        inserted = 0
        updated = 0
        changed: list[dict[str, object]] = []
        for item in observations:
            ref = str(item["reference_date"])
            value = float(str(item["valor"]))
            old_value = prior.get(ref)
            if old_value is None:
                inserted += 1
                changed.append({"data": str(item["data"]), "valor": value})
            elif old_value != value:
                raise CanaryAcquisitionError(
                    "canary cannot overwrite an existing SGS value without reviewed publication ordering"
                )
        return inserted, updated, changed

    dataset_key = profile.candidate_id.removeprefix("ibge_sidra:v3:")
    from db.sidra_store import query_sidra_history

    prior_rows = query_sidra_history(dataset_key)
    existing_by_period = {str(row["reference_period"]): row for row in prior_rows}
    inserted = 0
    updated = 0
    for item in observations:
        old = existing_by_period.get(str(item["reference_period"]))
        if old is None:
            inserted += 1
        elif (
            float(str(old["value"])) != float(str(item["value"]))
            or old["unit"] != item["unit"]
            or old["base_version"] != item["base_version"]
        ):
            raise CanaryAcquisitionError(
                "canary cannot add an economic SIDRA revision without reviewed publication ordering"
            )
    return inserted, updated, observations


def _sidra_reference_date(reference_period: str, frequency: str) -> str:
    """Convert registered SIDRA period codes to valid ISO reference dates."""
    if frequency == "annual" and len(reference_period) == 4:
        return f"{reference_period}-01-01"
    if frequency == "monthly" and len(reference_period) == 6:
        return f"{reference_period[:4]}-{reference_period[4:]}-01"
    if frequency == "quarterly" and len(reference_period) == 5:
        quarter = int(reference_period[-1])
        if quarter not in {1, 2, 3, 4}:
            raise ValueError("invalid SIDRA quarter reference")
        month = (quarter - 1) * 3 + 1
        return f"{reference_period[:4]}-{month:02d}-01"
    raise ValueError("unsupported registered SIDRA reference-period format")


def _persist_candidate_unit(
    profile: CandidateProfile,
    observations: list[dict[str, object]],
    changed_rows: list[dict[str, object]],
    captured_at: str,
    origins: list[tuple[object, ...]],
    receipt: tuple[object, ...],
    binding: store.CanaryBinding,
) -> None:
    if profile.adapter is ShadowAdapter.SGS_CSV:
        code = int(profile.candidate_id.rsplit(":", 1)[-1])
        store.upsert_sgs(
            code,
            cast(list[Record], changed_rows),
            first_seen_at=captured_at,
            origin_rows=origins,
            commit_receipt=receipt,
            canary_binding=binding,
        )
        return
    from db.sidra_store import insert_sidra_observations

    insert_sidra_observations(
        observations,
        first_seen_at=captured_at,
        origin_rows=origins,
        commit_receipt=receipt,
        canary_binding=binding,
    )
