"""Bounded paired primary/candidate acquisition for manually approved canaries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

from candidate_acquisition import (
    AcquiredBatch,
    CandidateAcquisitionService,
    approved_profile,
)
from candidate_eligibility import CandidateApproval
from ingestion_policy import ErrorCategory, classify_exception, current_retry_budget
from provider_hosts import require_allowed_provider_url
from source_candidates import CandidateProfile, ShadowAdapter


@dataclass(frozen=True, slots=True)
class CanaryPlan:
    candidate_id: str
    contract_id: str
    start: date
    end: date
    primary_url: str
    candidate_url: str
    primary_params: tuple[tuple[str, str], ...]
    candidate_params: tuple[tuple[str, str], ...]
    source: str = "bcb_sgs"
    reference_period: str | None = None


@dataclass(frozen=True, slots=True)
class CanaryComparison:
    plan: CanaryPlan
    primary: AcquiredBatch
    candidate: AcquiredBatch
    captured_at: str


class CanaryAcquisitionError(ValueError):
    """Paired canary acquisition or exact semantic comparison failed."""

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.SEMANTIC_CONFLICT,
    ) -> None:
        self.error_category = category
        super().__init__(message)


def build_sgs_plan(profile: CandidateProfile, as_of: date) -> CanaryPlan:
    """Bind one supported SGS series to a maximum 31-day request window."""
    if profile.adapter is not ShadowAdapter.SGS_CSV:
        raise CanaryAcquisitionError("only registered SGS CSV profiles are supported")
    code = _sgs_code(profile)
    if code == 226:
        raise CanaryAcquisitionError("TR226 has no qualified official dataFim capability")
    _validate_as_of(as_of)
    start = as_of - timedelta(days=30)
    start_text = start.strftime("%d/%m/%Y")
    end_text = as_of.strftime("%d/%m/%Y")
    return CanaryPlan(
        profile.candidate_id,
        f"sgs:{code}",
        start,
        as_of,
        require_allowed_provider_url(
            f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
        ),
        require_allowed_provider_url(
            f"https://{profile.endpoint_host}{(profile.endpoint_path_template or '').format(series_code=code)}"
        ),
        (("formato", "json"), ("dataInicial", start_text), ("dataFinal", end_text)),
        (*profile.fixed_parameters, ("dataInicial", start_text), ("dataFinal", end_text)),
    )


def build_sidra_plan(profile: CandidateProfile, as_of: date) -> CanaryPlan:
    """Build fixed SIDRA dataset request; actual latest period binds after primary parse."""
    if profile.adapter is not ShadowAdapter.SIDRA_V3:
        raise CanaryAcquisitionError("only registered SIDRA v3 profiles are supported")
    _validate_as_of(as_of)
    from datafetchers.ibge_sidra import SIDRA_DATASETS, build_sidra_url

    dataset_key = profile.candidate_id.removeprefix("ibge_sidra:v3:")
    spec = SIDRA_DATASETS.get(dataset_key)
    if spec is None or profile.candidate_id != f"ibge_sidra:v3:{spec.key}":
        raise CanaryAcquisitionError("SIDRA dataset is not registered")
    primary = require_allowed_provider_url(build_sidra_url(spec, period="last1"))
    candidate_path = (profile.endpoint_path_template or "").format(
        table_id=spec.table_id,
        period_range="last1",
        variable_id=spec.variable_id,
    )
    candidate = require_allowed_provider_url(f"https://{profile.endpoint_host}{candidate_path}")
    params = [("localidades", f"N1[{spec.territory_code}]")]
    if spec.classification_id is not None and spec.category_id is not None:
        params.append(("classificacao", f"{spec.classification_id}[{spec.category_id}]") )
    return CanaryPlan(
        profile.candidate_id,
        f"sidra:{spec.key}",
        as_of,
        as_of,
        primary,
        candidate,
        (),
        tuple(params),
        "ibge_sidra",
    )


def request_plan_sha256(plan: CanaryPlan, profile: CandidateProfile) -> str:
    """Hash the immutable request intent, excluding the later SIDRA cohort result."""
    payload = {
        "candidate_id": plan.candidate_id,
        "contract_id": plan.contract_id,
        "start": plan.start.isoformat(),
        "end": plan.end.isoformat(),
        "primary_url": plan.primary_url,
        "candidate_url": plan.candidate_url,
        "primary_params": plan.primary_params,
        "candidate_params": plan.candidate_params,
        "source": plan.source,
        "contract_version": profile.source_contract_version,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def acquire_sidra_canary(
    profile: CandidateProfile,
    approval: CandidateApproval,
    service: CandidateAcquisitionService,
    as_of: date,
) -> CanaryComparison:
    """Acquire and compare primary + v3 for the latest eligible SIDRA cohort."""
    dataset_key = profile.candidate_id.removeprefix("ibge_sidra:v3:")
    _require_approved(profile, approval, service, f"sidra:{dataset_key}")
    plan = build_sidra_plan(profile, as_of)
    budget = _require_budget()
    from datafetchers import ibge_sidra
    from provider_http import JSON_MAX_BYTES, get_provider_response
    from shadow_ingestion import parse_sidra_v3
    from source_ingestion import acquisition_unit

    spec = ibge_sidra.SIDRA_DATASETS[dataset_key]
    with acquisition_unit(
        f"canary:{profile.candidate_id}", 3, min(90.0, profile.deadline_seconds)
    ):
        try:
            primary_response = get_provider_response(
                plan.primary_url,
                timeout=min(profile.deadline_seconds, budget.remaining_seconds()),
                max_bytes=JSON_MAX_BYTES,
                follow_redirects=True,
            )
            primary_response.raise_for_status()
            primary_captured = datetime.now(timezone.utc).isoformat()
            primary_rows = ibge_sidra.parse_sidra_payload(
                spec, primary_response.content, first_seen_at=primary_captured
            )
            eligible = [
                row for row in primary_rows
                if row["reference_period"] <= f"{as_of.year:04d}{as_of.month:02d}"
            ]
            if not eligible:
                raise CanaryAcquisitionError("SIDRA primary has no eligible reference cohort")
            latest = max(row["reference_period"] for row in eligible)
            primary_rows = [row for row in eligible if row["reference_period"] == latest]
            candidate_path = (profile.endpoint_path_template or "").format(
                table_id=spec.table_id,
                period_range=latest,
                variable_id=spec.variable_id,
            )
            candidate_url = require_allowed_provider_url(
                f"https://{profile.endpoint_host}{candidate_path}"
            )
            candidate_response = get_provider_response(
                candidate_url,
                timeout=min(profile.deadline_seconds, budget.remaining_seconds()),
                max_bytes=profile.max_bytes,
                params=dict(plan.candidate_params),
                follow_redirects=True,
            )
            candidate_response.raise_for_status()
            candidate_captured = datetime.now(timezone.utc).isoformat()
            canonical = parse_sidra_v3(profile, spec, candidate_response.content, as_of=as_of)
            candidate_rows: list[ibge_sidra.SidraObservation] = []
            for item in canonical:
                if item.reference_date[:4] + item.reference_date[5:7] != latest:
                    continue
                values = dict(item.values)
                candidate_rows.append({
                    "dataset_key": spec.key,
                    "table_id": spec.table_id,
                    "variable_id": spec.variable_id,
                    "classification_id": spec.classification_id,
                    "category_id": spec.category_id,
                    "territory_level": spec.territory_level,
                    "territory_code": spec.territory_code,
                    "reference_period": latest,
                    "reference_label": primary_rows[0]["reference_label"],
                    "value": values["value"],
                    "unit": spec.unit,
                    "base_version": spec.base_version,
                    "source_published_at": None,
                    "first_seen_at": candidate_captured,
                    "frequency": spec.frequency,
                })
            primary = ibge_sidra.sidra_acquired_batch(
                primary_rows, sha256(primary_response.content).hexdigest(), primary_captured
            )
            candidate = ibge_sidra.sidra_acquired_batch(
                candidate_rows, sha256(candidate_response.content).hexdigest(), candidate_captured
            )
        except CanaryAcquisitionError:
            raise
        except Exception as exc:
            raise CanaryAcquisitionError(
                "paired SIDRA acquisition failed", category=classify_exception(exc)
            ) from exc

    if not primary.observations or _sidra_values(primary) != _sidra_values(candidate):
        raise CanaryAcquisitionError("primary and candidate SIDRA cohort disagree")
    actual_plan = CanaryPlan(
        plan.candidate_id, plan.contract_id, plan.start, plan.end,
        plan.primary_url, candidate_url, plan.primary_params, plan.candidate_params,
        plan.source, latest,
    )
    return CanaryComparison(actual_plan, primary, candidate, candidate_captured)


def acquire_sgs_canary(
    profile: CandidateProfile,
    approval: CandidateApproval,
    service: CandidateAcquisitionService,
    as_of: date,
) -> CanaryComparison:
    """Acquire and exactly compare one primary SGS window and its CSV alternative."""
    code = _sgs_code(profile)
    _require_approved(profile, approval, service, f"sgs:{code}")
    plan = build_sgs_plan(profile, as_of)
    budget = _require_budget()
    from datafetchers import bcb_sgs
    from provider_http import get_provider_response
    from shadow_ingestion import parse_sgs_csv
    from source_ingestion import acquisition_unit

    start_text = plan.start.strftime("%d/%m/%Y")
    end_text = plan.end.strftime("%d/%m/%Y")
    with acquisition_unit(
        f"canary:{profile.candidate_id}", 3, min(20.0, profile.deadline_seconds)
    ):
        try:
            primary_response = get_provider_response(
                plan.primary_url,
                timeout=min(profile.deadline_seconds, budget.remaining_seconds()),
                max_bytes=256 * 1024,
                params=dict(plan.primary_params),
            )
            primary_response.raise_for_status()
            primary_captured = datetime.now(timezone.utc).isoformat()
            primary_payload = json.loads(primary_response.content)
            primary_records = bcb_sgs.parse_primary_sgs_window(
                primary_payload, code, start_text, end_text
            )
            primary = bcb_sgs.sgs_acquired_batch(
                primary_records, code, sha256(primary_response.content).hexdigest(), primary_captured
            )
            candidate_response = get_provider_response(
                plan.candidate_url,
                timeout=min(profile.deadline_seconds, budget.remaining_seconds()),
                max_bytes=profile.max_bytes,
                params=dict(plan.candidate_params),
            )
            candidate_response.raise_for_status()
            candidate_captured = datetime.now(timezone.utc).isoformat()
            parsed = parse_sgs_csv(profile, candidate_response.content, as_of=as_of)
            candidate_records = bcb_sgs.candidate_sgs_records(
                parsed, code, plan.start, plan.end
            )
            candidate = bcb_sgs.sgs_acquired_batch(
                candidate_records, code, sha256(candidate_response.content).hexdigest(), candidate_captured
            )
        except CanaryAcquisitionError:
            raise
        except Exception as exc:
            raise CanaryAcquisitionError(
                "paired SGS acquisition failed", category=classify_exception(exc)
            ) from exc

    if not primary.observations or not candidate.observations:
        raise CanaryAcquisitionError("primary and candidate SGS windows must be nonempty")
    if _sgs_values(primary) != _sgs_values(candidate):
        raise CanaryAcquisitionError("primary and candidate SGS windows disagree")
    return CanaryComparison(plan, primary, candidate, candidate_captured)


def _require_approved(
    profile: CandidateProfile,
    approval: CandidateApproval,
    service: CandidateAcquisitionService,
    contract_id: str,
) -> None:
    resolved, registered_approval, blocker = approved_profile(
        profile.candidate_id,
        contract_id,
        profiles=service.profiles,
        approvals=service.approvals,
    )
    if blocker or resolved != profile or registered_approval != approval:
        raise CanaryAcquisitionError(
            f"candidate is not approved: {blocker or 'registry mismatch'}"
        )


def _require_budget():
    budget = current_retry_budget()
    if budget is None:
        raise CanaryAcquisitionError("shared RetryBudget is required")
    return budget


def _validate_as_of(as_of: date) -> None:
    if as_of > datetime.now(timezone.utc).date():
        raise CanaryAcquisitionError("as_of cannot be in the future")


def _sgs_code(profile: CandidateProfile) -> int:
    try:
        return int(profile.candidate_id.rsplit(":", 1)[-1])
    except ValueError as exc:
        raise CanaryAcquisitionError("invalid SGS candidate identity") from exc


def _sgs_values(batch: AcquiredBatch) -> tuple[tuple[object, object, object], ...]:
    rows = tuple(
        (item.get("series_code"), item.get("reference_date"), item.get("valor"))
        for item in batch.observations
    )
    if len(rows) != len(set(rows)):
        raise CanaryAcquisitionError("duplicate canonical SGS observations")
    return tuple(sorted(rows, key=repr))


def _sidra_values(batch: AcquiredBatch) -> tuple[tuple[object, ...], ...]:
    fields = (
        "dataset_key", "table_id", "variable_id", "classification_id", "category_id",
        "territory_level", "territory_code", "reference_period", "value", "unit",
        "base_version", "frequency",
    )
    values = (tuple(item.get(field) for field in fields) for item in batch.observations)
    return tuple(sorted(values, key=repr))
