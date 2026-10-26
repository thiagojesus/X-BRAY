"""Read-only candidate acquisition and equivalence evidence for Phase 3.

This module deliberately bypasses source refresh callbacks: they persist data
inside their fetch paths. Shadow reads use the shared bounded HTTP transport but
only retain local evidence artifacts; they never write source tables or ledgers.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Final, TypeGuard

import httpx

from datafetchers.bcb_sgs import parse_sgs_shadow_primary_payload
from datafetchers.ibge_sidra import (
    SIDRA_DATASETS,
    SidraDatasetSpec,
    build_sidra_url,
    parse_sidra_payload,
)
from ingestion_policy import (
    ErrorCategory,
    RetryBudget,
    RetryProfile,
    classify_exception,
    current_retry_budget,
    install_retry_budget,
    reset_retry_budget,
)
from provider_hosts import require_allowed_provider_url
from provider_http import ProviderResponse, get_provider_response
from source_candidates import (
    CANDIDATE_CODE_VERSION,
    CandidateProfile,
    CandidateStatus,
    ShadowAdapter,
    get_candidate_profile,
)
from source_contracts import CONTRACT_BY_ID, SCHEMA_VERSION, Source
from source_ingestion import acquisition_unit

EVIDENCE_SCHEMA_VERSION: Final = "equivalence-evidence-v1"
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


def _json_array(value: JsonValue) -> TypeGuard[list[JsonValue]]:
    return isinstance(value, list)


def _json_object(value: JsonValue) -> TypeGuard[JsonObject]:
    return isinstance(value, dict)
MIN_DISTINCT_REFERENCES: Final = 10
SGS_SAMPLE_MONTHS: Final = 18
SIDRA_SAMPLE_MONTHS: Final = 18
_MONTH_PATTERN: Final = re.compile(r"^\d{6}$")
_MISSING_SIDRA_VALUES: Final = frozenset({"", "...", "X"})


@dataclass(frozen=True, slots=True)
class CanonicalObservation:
    contract_id: str
    reference_date: str
    identity: tuple[tuple[str, str], ...]
    values: tuple[tuple[str, float], ...]
    unit: str
    frequency: str
    source_published_at: str | None = None

    def identity_key(self) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        return self.contract_id, self.reference_date, self.identity


@dataclass(frozen=True, slots=True)
class CandidateBatch:
    profile_id: str
    contract_ids: tuple[str, ...]
    observations: tuple[CanonicalObservation, ...]
    payload_sha256: str
    request_parameters_sha256: str
    endpoint_host: str


@dataclass(frozen=True, slots=True)
class ShadowRequestPlan:
    primary_url: str
    candidate_url: str
    primary_params: dict[str, str]
    candidate_params: dict[str, str]


@dataclass(frozen=True, slots=True)
class EquivalenceEvidence:
    schema_version: str
    evidence_status: str
    source_contract_version: str
    candidate_profile_id: str
    candidate_profile_sha256: str
    candidate_code_version: str
    as_of: str
    required_distinct_references: int
    primary_distinct_references: int
    candidate_distinct_references: int
    latest_primary_reference: str | None
    latest_candidate_reference: str | None
    matched_observation_count: int
    mismatch_count: int
    mismatches: tuple[str, ...]
    production_eligible: bool
    eligibility_blockers: tuple[str, ...]
    primary_request_parameters_sha256: str
    candidate_request_parameters_sha256: str
    primary_payload_sha256: str
    candidate_payload_sha256: str
    primary_observations: tuple[CanonicalObservation, ...]
    candidate_observations: tuple[CanonicalObservation, ...]


class ShadowRunError(RuntimeError):
    def __init__(self, category: ErrorCategory, code: str) -> None:
        self.error_category = category
        self.code = code
        super().__init__(code)


def _month_range(as_of: date, months: int) -> tuple[str, str]:
    start_index = as_of.year * 12 + as_of.month - 1 - (months - 1)
    start_year, start_month_zero = divmod(start_index, 12)
    start = date(start_year, start_month_zero + 1, 1)
    end = date(as_of.year, as_of.month, 1)
    return start.strftime("%Y%m"), end.strftime("%Y%m")


def _number(value: object, *, decimal_comma: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean value is not numeric")  # noqa: TRY004
    if isinstance(value, int | float):
        result = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if decimal_comma and "," in text:
            text = text.replace(".", "").replace(",", ".")
        result = float(text)
    else:
        raise ValueError("value is not numeric")  # noqa: TRY004
    if not math.isfinite(result):
        raise ValueError("value is not finite")
    return result


def _reference_day(raw: str, *, as_of: date) -> date:
    parsed = datetime.strptime(raw, "%d/%m/%Y").date()  # noqa: DTZ007
    if parsed.strftime("%d/%m/%Y") != raw:
        raise ValueError("reference date is not canonical dd/mm/YYYY")
    if parsed > as_of:
        raise ValueError("source reference date is after the as-of cutoff")
    return parsed


def _sgs_observations(
    profile: CandidateProfile, payload: object, *, as_of: date
) -> tuple[CanonicalObservation, ...]:
    contract_id = profile.source_contract_ids[0]
    series_code = int(contract_id.removeprefix("sgs:"))
    records = parse_sgs_shadow_primary_payload(payload, series_code)
    contract = CONTRACT_BY_ID[contract_id]
    result: list[CanonicalObservation] = []
    seen: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for record in records:
        raw_start = record.get("data")
        if not isinstance(raw_start, str):
            raise ValueError("primary SGS row is missing a source reference date")  # noqa: TRY004
        start = _reference_day(raw_start, as_of=as_of)
        value = _number(record.get("valor"), decimal_comma=True)
        if series_code == 226:
            raw_end = record.get("dataFim")
            if not isinstance(raw_end, str):
                raise ValueError("TR observation lacks full period end")
            end = datetime.strptime(raw_end, "%d/%m/%Y").date()  # noqa: DTZ007
            if end.strftime("%d/%m/%Y") != raw_end or end <= start:
                raise ValueError("TR period end is not valid for its start")
            identity = (("series_code", str(series_code)),
                        ("period_start", start.isoformat()),
                        ("period_end", end.isoformat()))
        else:
            identity = (("series_code", str(series_code)), ("reference_date", start.isoformat()))
        key = (start.isoformat(), identity)
        prior = seen.get(key)
        if prior is not None and not math.isclose(prior, value, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("conflicting duplicate SGS identity")
        if prior is not None:
            continue
        seen[key] = value
        result.append(CanonicalObservation(
            contract_id=contract_id,
            reference_date=start.isoformat(),
            identity=identity,
            values=((contract.value_fields[0], value),),
            unit=contract.unit,
            frequency=contract.frequency,
        ))
    return tuple(result)


def parse_sgs_csv(
    profile: CandidateProfile, content: bytes, *, as_of: date
) -> tuple[CanonicalObservation, ...]:
    """Strict decoder for the declared BCB CSV profile; no inferred TR end."""
    if profile.adapter is not ShadowAdapter.SGS_CSV:
        raise ValueError("candidate profile does not declare SGS CSV")
    contract_id = profile.source_contract_ids[0]
    series_code = int(contract_id.removeprefix("sgs:"))
    reader = csv.DictReader(content.decode("utf-8-sig").splitlines(), delimiter=";")
    required = {"data", "valor"}
    if series_code == 226:
        required.add("dataFim")
    if reader.fieldnames is None or set(reader.fieldnames) != required:
        raise ValueError("SGS CSV header does not match the declared contract")
    contract = CONTRACT_BY_ID[contract_id]
    result: list[CanonicalObservation] = []
    seen: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for row in reader:
        raw_date = row.get("data")
        raw_value = row.get("valor")
        if raw_date is None or raw_value is None or not raw_value.strip():
            raise ValueError("SGS CSV row is missing a required field")
        start = _reference_day(raw_date, as_of=as_of)
        value = _number(raw_value, decimal_comma=True)
        if series_code == 226:
            raw_end = row.get("dataFim")
            if not raw_end:
                raise ValueError("TR CSV row lacks full period end")
            end = datetime.strptime(raw_end, "%d/%m/%Y").date()  # noqa: DTZ007
            if end.strftime("%d/%m/%Y") != raw_end or end <= start:
                raise ValueError("TR CSV period end is invalid")
            identity = (("series_code", str(series_code)),
                        ("period_start", start.isoformat()),
                        ("period_end", end.isoformat()))
        else:
            identity = (("series_code", str(series_code)), ("reference_date", start.isoformat()))
        key = (start.isoformat(), identity)
        if key in seen:
            if not math.isclose(seen[key], value, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("conflicting duplicate SGS CSV identity")
            continue
        seen[key] = value
        result.append(CanonicalObservation(
            contract_id=contract_id,
            reference_date=start.isoformat(),
            identity=identity,
            values=((contract.value_fields[0], value),),
            unit=contract.unit,
            frequency=contract.frequency,
        ))
    if not result:
        raise ValueError("SGS CSV contains no usable observations")
    return tuple(result)


def _sidra_observations(
    profile: CandidateProfile,
    spec: SidraDatasetSpec,
    rows: list[dict[str, object]],
    *,
    as_of: date,
) -> tuple[CanonicalObservation, ...]:
    contract_id = profile.source_contract_ids[0]
    contract = CONTRACT_BY_ID[contract_id]
    result: list[CanonicalObservation] = []
    seen: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for row in rows:
        period = row.get("reference_period")
        if not isinstance(period, str) or _MONTH_PATTERN.fullmatch(period) is None:
            raise ValueError("SIDRA reference period must be YYYYMM")
        period_start = date(int(period[:4]), int(period[4:]), 1)
        if period_start > date(as_of.year, as_of.month, 1):
            continue
        identity = tuple((field, str(row[field])) for field in contract.identity)
        value = _number(row.get("value"))
        unit = str(row.get("unit"))
        frequency = str(row.get("frequency"))
        key = (period, identity)
        if key in seen:
            if not math.isclose(seen[key], value, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("conflicting duplicate SIDRA identity")
            continue
        seen[key] = value
        result.append(CanonicalObservation(
            contract_id=contract_id,
            reference_date=period_start.isoformat(),
            identity=identity,
            values=((contract.value_fields[0], value),),
            unit=unit,
            frequency=frequency,
        ))
    if not result:
        raise ValueError("SIDRA response contains no usable observations")
    return tuple(result)


def parse_sidra_v3(
    profile: CandidateProfile, spec: SidraDatasetSpec, content: bytes, *, as_of: date
) -> tuple[CanonicalObservation, ...]:
    """Decode only the documented v3 aggregate structure, fail closed on gaps."""
    if profile.adapter is not ShadowAdapter.SIDRA_V3:
        raise ValueError("candidate profile does not declare SIDRA v3")
    # Python's JSON decoder returns only JSON-native values; this annotation
    # establishes a typed boundary before schema-specific TypeGuard validation.
    decoded: JsonValue = json.loads(content)
    payload = decoded
    if not _json_array(payload) or len(payload) != 1 or not _json_object(payload[0]):
        raise ValueError("SIDRA v3 must return exactly the requested aggregate")
    aggregate = payload[0]
    if str(aggregate.get("id")) != str(spec.table_id):
        raise ValueError("SIDRA v3 table id does not match the profile")
    if str(aggregate.get("unidade")) != spec.unit_label:
        raise ValueError("SIDRA v3 unit does not match the profile")
    if str(aggregate.get("variavel")) != spec.variable_label:
        raise ValueError("SIDRA v3 variable does not match the profile")
    results = aggregate.get("resultados")
    if not isinstance(results, list) or not results:
        raise ValueError("SIDRA v3 results are absent")
    rows: list[dict[str, object]] = []
    for group in results:
        if not _json_object(group):
            raise ValueError("SIDRA v3 result group is malformed")
        classifications = group.get("classificacoes", [])
        if not _json_array(classifications):
            raise ValueError("SIDRA v3 classifications are malformed")
        if spec.classification_id is None:
            if classifications:
                raise ValueError("SIDRA v3 supplied an undeclared classification")
            category_label = None
        else:
            category_label = _sidra_category(classifications, spec)
        series = group.get("series")
        if not _json_array(series) or not series:
            raise ValueError("SIDRA v3 series are absent")
        for item in series:
            rows.extend(_sidra_v3_series_rows(item, spec, category_label))
    return _sidra_observations(profile, spec, rows, as_of=as_of)


def _sidra_category(classifications: list[JsonValue], spec: SidraDatasetSpec) -> str:
    for item in classifications:
        if not _json_object(item) or str(item.get("id")) != str(spec.classification_id):
            continue
        categories = item.get("categoria")
        if _json_object(categories):
            category = categories.get(str(spec.category_id))
            if isinstance(category, str) and category:
                return category
    raise ValueError("SIDRA v3 category does not match the dataset contract")


def _sidra_v3_series_rows(
    item: JsonValue, spec: SidraDatasetSpec, category_label: str | None
) -> list[dict[str, object]]:
    if not _json_object(item):
        raise ValueError("SIDRA v3 series item is malformed")
    locality = item.get("localidade")
    series = item.get("serie")
    if not _json_object(locality) or not _json_object(series):
        raise ValueError("SIDRA v3 locality or values are absent")
    level = locality.get("nivel")
    if not _json_object(level):
        raise ValueError("SIDRA v3 locality level is absent")
    if (
        str(locality.get("id")) != spec.territory_code
        or str(locality.get("nome")) != "Brasil"
        or str(level.get("id")) != spec.territory_level
    ):
        raise ValueError("SIDRA v3 geography is not Brazil N1")
    rows: list[dict[str, object]] = []
    for period, raw_value in series.items():
        if _MONTH_PATTERN.fullmatch(period) is None:
            raise ValueError("SIDRA v3 period is not YYYYMM")
        if not isinstance(raw_value, str):
            raise ValueError("SIDRA v3 value must be a string")  # noqa: TRY004
        if raw_value in _MISSING_SIDRA_VALUES:
            continue
        row: dict[str, object] = {
            "dataset_key": spec.key,
            "table_id": spec.table_id,
            "variable_id": spec.variable_id,
            "classification_id": spec.classification_id,
            "category_id": spec.category_id,
            "territory_level": spec.territory_level,
            "territory_code": spec.territory_code,
            "reference_period": period,
            "unit": spec.unit,
            "base_version": spec.base_version,
            "frequency": spec.frequency,
            "value": _number("0" if raw_value == "-" else raw_value, decimal_comma=True),
        }
        if category_label is not None:
            row["category_label"] = category_label
        rows.append(row)
    return rows


def _sidra_v1_primary(
    profile: CandidateProfile, spec: SidraDatasetSpec, content: bytes, *, as_of: date
) -> tuple[CanonicalObservation, ...]:
    content = _sidra_v1_shadow_content(content)
    parsed = parse_sidra_payload(
        spec, content, first_seen_at=datetime.now(timezone.utc).isoformat()
    )
    return _sidra_observations(profile, spec, [dict(row) for row in parsed], as_of=as_of)


def _sidra_v1_shadow_content(content: bytes) -> bytes:
    """Preserve SIDRA's exact dash-as-zero convention in shadow primary data.

    This is scoped to SIDRA's documented representation: no generic numeric
    parser changes and no sentinel such as ``...`` or ``X`` is imputed.
    """
    decoded: JsonValue = json.loads(content)
    if not _json_array(decoded):
        return content
    for row in decoded[1:]:
        if _json_object(row) and row.get("V") == "-":
            row["V"] = "0"
    return json.dumps(decoded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _params_hash(params: dict[str, str], endpoint: str) -> str:
    canonical = json.dumps(
        {"endpoint": _safe_endpoint(endpoint), "parameters": params},
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode()).hexdigest()


def _collect_sgs(
    profile: CandidateProfile, code: int, as_of: date
) -> tuple[CandidateBatch, CandidateBatch, ShadowRequestPlan]:
    start_date = as_of - timedelta(days=SGS_SAMPLE_MONTHS * 31)
    date_params = {
        "dataInicial": start_date.strftime("%d/%m/%Y"),
        "dataFinal": as_of.strftime("%d/%m/%Y"),
    }
    root = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
    primary_params = {"formato": "json", **date_params}
    csv_params = {**dict(profile.fixed_parameters), **date_params}
    primary_url = require_allowed_provider_url(root.format(code=code))
    candidate_url = require_allowed_provider_url(
        f"https://{profile.endpoint_host}{(profile.endpoint_path_template or '').format(series_code=code)}"
    )
    primary_response = _shadow_request(profile, "shadow:primary", primary_url, primary_params)
    primary_observations = _sgs_observations(
        profile, json.loads(primary_response.content), as_of=as_of
    )
    candidate_response = _shadow_request(profile, "shadow:candidate", candidate_url, csv_params)
    candidate_observations = parse_sgs_csv(profile, candidate_response.content, as_of=as_of)
    primary = CandidateBatch(
        profile.candidate_id, profile.source_contract_ids, primary_observations,
        sha256(primary_response.content).hexdigest(), _params_hash(primary_params, primary_url), "api.bcb.gov.br",
    )
    candidate = CandidateBatch(
        profile.candidate_id, profile.source_contract_ids, candidate_observations,
        sha256(candidate_response.content).hexdigest(), _params_hash(csv_params, candidate_url), profile.endpoint_host or "",
    )
    return primary, candidate, ShadowRequestPlan(
        primary_url, candidate_url, primary_params, csv_params
    )


def _collect_sidra(
    profile: CandidateProfile, as_of: date
) -> tuple[CandidateBatch, CandidateBatch, ShadowRequestPlan]:
    contract_id = profile.source_contract_ids[0]
    dataset_key = contract_id.removeprefix("sidra:")
    spec = SIDRA_DATASETS[dataset_key]
    period_start, period_end = _month_range(as_of, SIDRA_SAMPLE_MONTHS)
    period_range = f"{period_start}-{period_end}"
    primary_url = require_allowed_provider_url(build_sidra_url(spec, period=period_range))
    candidate_url = require_allowed_provider_url(
        "https://servicodados.ibge.gov.br"
        + (profile.endpoint_path_template or "").format(
            table_id=spec.table_id, period_range=period_range, variable_id=spec.variable_id
        )
    )
    v3_params = {"localidades": f"N1[{spec.territory_code}]"}
    if spec.classification_id is not None and spec.category_id is not None:
        v3_params["classificacao"] = f"{spec.classification_id}[{spec.category_id}]"
    primary_plan = {"formato": "json", "period": period_range}
    primary_response = _shadow_request(profile, "shadow:primary", primary_url, {})
    primary_observations = _sidra_v1_primary(
        profile, spec, primary_response.content, as_of=as_of
    )
    candidate_response = _shadow_request(profile, "shadow:candidate", candidate_url, v3_params)
    candidate_observations = parse_sidra_v3(profile, spec, candidate_response.content, as_of=as_of)
    primary = CandidateBatch(
        profile.candidate_id, profile.source_contract_ids, primary_observations,
        sha256(primary_response.content).hexdigest(), _params_hash(primary_plan, primary_url),
        "apisidra.ibge.gov.br",
    )
    candidate = CandidateBatch(
        profile.candidate_id, profile.source_contract_ids, candidate_observations,
        sha256(candidate_response.content).hexdigest(), _params_hash(v3_params, candidate_url),
        profile.endpoint_host or "",
    )
    return primary, candidate, ShadowRequestPlan(
        primary_url, candidate_url, primary_plan, v3_params
    )


def _batch_map(batch: CandidateBatch, as_of: date) -> dict[tuple[str, str, tuple[tuple[str, str], ...]], CanonicalObservation]:
    expected = set(batch.contract_ids)
    if not expected:
        raise ValueError("candidate has no source contract")
    result: dict[tuple[str, str, tuple[tuple[str, str], ...]], CanonicalObservation] = {}
    for observation in batch.observations:
        if observation.contract_id not in expected:
            raise ValueError("batch contains an unplanned contract")
        reference_date = date.fromisoformat(observation.reference_date)
        if reference_date.isoformat() != observation.reference_date:
            raise ValueError("reference date is not canonical ISO-8601")
        if reference_date > as_of:
            raise ValueError("batch includes a post-cutoff reference point")
        if not observation.identity or not observation.values:
            raise ValueError("batch observation is missing contract dimensions")
        contract = CONTRACT_BY_ID[observation.contract_id]
        if {field for field, _ in observation.identity} != set(contract.identity):
            raise ValueError("observation identity does not cover the full source contract")
        if {field for field, _ in observation.values} != set(contract.value_fields):
            raise ValueError("observation value fields do not cover the source contract")
        if observation.unit != contract.unit or observation.frequency != contract.frequency:
            raise ValueError("observation unit or frequency differs from the source contract")
        if contract.source_published_at_field is None and observation.source_published_at is not None:
            raise ValueError("capture time cannot be presented as source publication time")
        if any(not math.isfinite(value) for _, value in observation.values):
            raise ValueError("observation contains a non-finite value")
        key = observation.identity_key()
        if key in result:
            raise ValueError("duplicate canonical observation identity")
        result[key] = observation
    return result


def _shadow_request(
    profile: CandidateProfile, unit_id: str, url: str, params: dict[str, str]
) -> ProviderResponse:
    with _budget_unit(unit_id, profile.request_cap, profile.deadline_seconds):
        return get_provider_response(
            url,
            timeout=profile.deadline_seconds,
            max_bytes=profile.max_bytes,
            params=params,
        )


def compare_batches(
    profile: CandidateProfile,
    primary: CandidateBatch,
    candidate: CandidateBatch,
    *,
    as_of: date,
) -> EquivalenceEvidence:
    mismatch: list[str] = []
    if profile.source_contract_version != SCHEMA_VERSION:
        mismatch.append("stale_source_contract_version")
    if profile.code_version != CANDIDATE_CODE_VERSION:
        mismatch.append("stale_candidate_code_version")
    if primary.contract_ids != profile.source_contract_ids or candidate.contract_ids != profile.source_contract_ids:
        mismatch.append("contract_coverage_mismatch")
    if primary.profile_id != profile.candidate_id or candidate.profile_id != profile.candidate_id:
        mismatch.append("candidate_profile_id_mismatch")
    if candidate.endpoint_host != profile.endpoint_host:
        mismatch.append("candidate_endpoint_host_mismatch")
    expected_primary_host = {
        Source.SGS: "api.bcb.gov.br",
        Source.SIDRA: "apisidra.ibge.gov.br",
    }.get(profile.source)
    if expected_primary_host is not None and primary.endpoint_host != expected_primary_host:
        mismatch.append("primary_endpoint_host_mismatch")
    if any(
        re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for digest in (
            primary.payload_sha256,
            candidate.payload_sha256,
            primary.request_parameters_sha256,
            candidate.request_parameters_sha256,
        )
    ):
        mismatch.append("evidence_hash_invalid")
    primary_map = _batch_map(primary, as_of)
    candidate_map = _batch_map(candidate, as_of)
    primary_dates = {item.reference_date for item in primary_map.values()}
    candidate_dates = {item.reference_date for item in candidate_map.values()}
    primary_latest = max(primary_dates, default=None)
    candidate_latest = max(candidate_dates, default=None)
    if len(primary_dates) < MIN_DISTINCT_REFERENCES:
        mismatch.append("primary_has_fewer_than_10_distinct_reference_dates")
    if len(candidate_dates) < MIN_DISTINCT_REFERENCES:
        mismatch.append("candidate_has_fewer_than_10_distinct_reference_dates")
    if primary_latest != candidate_latest:
        mismatch.append("latest_reference_date_mismatch")
    if not primary_latest or not candidate_latest:
        mismatch.append("no_latest_reference_date")
    primary_keys = set(primary_map)
    candidate_keys = set(candidate_map)
    if primary_keys != candidate_keys:
        mismatch.append("full_identity_cohort_mismatch")
    point_errors: list[str] = []
    for key in sorted(primary_keys & candidate_keys):
        p = primary_map[key]
        c = candidate_map[key]
        if p.unit != c.unit or p.frequency != c.frequency:
            point_errors.append(f"{p.reference_date}:unit_or_frequency")
            continue
        p_values = dict(p.values)
        c_values = dict(c.values)
        for value_field in CONTRACT_BY_ID[p.contract_id].value_fields:
            if value_field not in p_values or value_field not in c_values:
                point_errors.append(f"{p.reference_date}:missing_{value_field}")
            elif not math.isclose(p_values[value_field], c_values[value_field], rel_tol=1e-7, abs_tol=1e-8):
                point_errors.append(f"{p.reference_date}:value_mismatch_{value_field}")
    mismatch.extend(point_errors[:50])
    if len(point_errors) > 50:
        mismatch.append("additional_point_mismatches_omitted")
    blockers = _eligibility_blockers(profile, mismatch)
    passed = not mismatch
    return EquivalenceEvidence(
        schema_version=EVIDENCE_SCHEMA_VERSION,
        evidence_status="evidence_passed" if passed else "evidence_failed",
        source_contract_version=profile.source_contract_version,
        candidate_profile_id=profile.candidate_id,
        candidate_profile_sha256=profile.profile_sha256(),
        candidate_code_version=profile.code_version,
        as_of=as_of.isoformat(),
        required_distinct_references=MIN_DISTINCT_REFERENCES,
        primary_distinct_references=len(primary_dates),
        candidate_distinct_references=len(candidate_dates),
        latest_primary_reference=primary_latest,
        latest_candidate_reference=candidate_latest,
        matched_observation_count=max(0, len(primary_keys & candidate_keys) - len(point_errors)),
        mismatch_count=len(mismatch),
        mismatches=tuple(mismatch),
        production_eligible=passed and profile.ingestion_eligible,
        eligibility_blockers=blockers,
        primary_request_parameters_sha256=primary.request_parameters_sha256,
        candidate_request_parameters_sha256=candidate.request_parameters_sha256,
        primary_payload_sha256=primary.payload_sha256,
        candidate_payload_sha256=candidate.payload_sha256,
        primary_observations=primary.observations,
        candidate_observations=candidate.observations,
    )


def _eligibility_blockers(profile: CandidateProfile, mismatches: list[str]) -> tuple[str, ...]:
    blockers: list[str] = []
    if mismatches:
        blockers.append("equivalence_evidence_not_passed")
    if profile.license_status != "verified" or profile.license_evidence_url is None:
        blockers.append("license_evidence_unverified")
    if not profile.code_reviewed:
        blockers.append("candidate_code_review_required")
    if profile.approved_proof_sha256 is None:
        blockers.append("reviewed_proof_hash_not_bound")
    if not profile.enabled_for_ingestion:
        blockers.append("production_ingestion_disabled")
    return tuple(blockers)


def _observation_json(observation: CanonicalObservation) -> JsonObject:
    return {
        "contract_id": observation.contract_id,
        "reference_date": observation.reference_date,
        "identity": {key: value for key, value in observation.identity},
        "values": {key: value for key, value in observation.values},
        "unit": observation.unit,
        "frequency": observation.frequency,
        "source_published_at": observation.source_published_at,
    }


def _evidence_json(evidence: EquivalenceEvidence) -> JsonObject:
    return {
        "schema_version": evidence.schema_version,
        "evidence_status": evidence.evidence_status,
        "source_contract_version": evidence.source_contract_version,
        "candidate_profile_id": evidence.candidate_profile_id,
        "candidate_profile_sha256": evidence.candidate_profile_sha256,
        "candidate_code_version": evidence.candidate_code_version,
        "as_of": evidence.as_of,
        "required_distinct_references": evidence.required_distinct_references,
        "primary_distinct_references": evidence.primary_distinct_references,
        "candidate_distinct_references": evidence.candidate_distinct_references,
        "latest_primary_reference": evidence.latest_primary_reference,
        "latest_candidate_reference": evidence.latest_candidate_reference,
        "matched_observation_count": evidence.matched_observation_count,
        "mismatch_count": evidence.mismatch_count,
        "mismatches": list(evidence.mismatches),
        "production_eligible": evidence.production_eligible,
        "eligibility_blockers": list(evidence.eligibility_blockers),
        "primary_request_parameters_sha256": evidence.primary_request_parameters_sha256,
        "candidate_request_parameters_sha256": evidence.candidate_request_parameters_sha256,
        "primary_payload_sha256": evidence.primary_payload_sha256,
        "candidate_payload_sha256": evidence.candidate_payload_sha256,
        "primary_observations": [_observation_json(item) for item in evidence.primary_observations],
        "candidate_observations": [_observation_json(item) for item in evidence.candidate_observations],
    }


def _write_private_artifact(path: Path, payload: Mapping[str, object]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError("shadow artifact path already exists; choose a new path")
    serialized = json.dumps(payload, sort_keys=True, indent=2, default=str).encode("utf-8") + b"\n"
    fd, temporary_name = tempfile.mkstemp(prefix=".shadow-evidence-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as artifact:
            artifact.write(serialized)
            artifact.flush()
            os.fsync(artifact.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _failure_artifact(
    profile: CandidateProfile, as_of: date, error: Exception, *, requests: int, retries: int
) -> dict[str, object]:
    category = classify_exception(error)
    retry_at = getattr(error, "retry_not_before", None)
    detail: dict[str, object] = {
        "type": type(error).__name__,
        "category": category.value,
        "code": getattr(error, "code", "shadow_acquisition_failed"),
    }
    if isinstance(retry_at, datetime):
        detail["retry_not_before"] = retry_at.astimezone(timezone.utc).isoformat()
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "mode": "shadow",
        "candidate_id": profile.candidate_id,
        "candidate_profile_sha256": profile.profile_sha256(),
        "source_contract_version": profile.source_contract_version,
        "candidate_code_version": profile.code_version,
        "as_of": as_of.isoformat(),
        "status": "unsupported" if profile.adapter is None else "acquisition_failed",
        "error": detail,
        "network": {"actual_outbound_requests": requests, "retry_requests": retries},
        "source_table_writes": 0,
        "production_eligible": False,
        "eligibility_blockers": ["shadow_run_not_equivalent", *_eligibility_blockers(profile, ["failed"])],
    }


def run_shadow(candidate_id: str, as_of: date, artifact_path: Path) -> dict[str, object]:
    """Acquire and compare one catalog profile under a single request budget."""
    profile = get_candidate_profile(candidate_id)
    if as_of > datetime.now(timezone.utc).date():
        raise ValueError("as-of date cannot be in the future")
    if profile.adapter is None:
        artifact = _failure_artifact(profile, as_of, ShadowRunError(ErrorCategory.CONFIGURATION_ERROR, "candidate_adapter_unavailable"), requests=0, retries=0)
        _write_private_artifact(artifact_path, artifact)
        return artifact
    if profile.status not in {CandidateStatus.SHADOW, CandidateStatus.UNVERIFIED}:
        artifact = _failure_artifact(profile, as_of, ShadowRunError(ErrorCategory.CONFIGURATION_ERROR, "candidate_not_shadow_enabled"), requests=0, retries=0)
        _write_private_artifact(artifact_path, artifact)
        return artifact
    budget_profile = RetryProfile(
        source=profile.source.value,
        max_requests=profile.request_cap,
        deadline_seconds=profile.deadline_seconds,
        request_timeout_seconds=profile.deadline_seconds,
    )
    budget = RetryBudget(budget_profile, scope_id=f"{profile.quota_scope}:primary:primary")
    token = install_retry_budget(budget)
    try:
        if profile.adapter is ShadowAdapter.SGS_CSV:
            code = int(profile.candidate_id.rsplit(":", 1)[1])
            primary, candidate, requests = _collect_sgs(profile, code, as_of)
        elif profile.adapter is ShadowAdapter.SIDRA_V3:
            primary, candidate, requests = _collect_sidra(profile, as_of)
        else:
            raise ShadowRunError(ErrorCategory.CONFIGURATION_ERROR, "unsupported_candidate_adapter")
        evidence = compare_batches(profile, primary, candidate, as_of=as_of)
        artifact: dict[str, object] = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "mode": "shadow",
            "status": evidence.evidence_status,
            "candidate": _profile_public_dict(profile),
            "candidate_profile_sha256": profile.profile_sha256(),
            "source_contract_version": SCHEMA_VERSION,
            "candidate_code_version": CANDIDATE_CODE_VERSION,
            "as_of": as_of.isoformat(),
            "request_plan_sha256": {
                "primary": evidence.primary_request_parameters_sha256,
                "candidate": evidence.candidate_request_parameters_sha256,
            },
            "requested_endpoints": {
                "primary": _safe_endpoint(requests.primary_url),
                "candidate": _safe_endpoint(requests.candidate_url),
            },
            "network": {"actual_outbound_requests": budget.requests, "retry_requests": budget.retries},
            "source_table_writes": 0,
            "evidence": _evidence_json(evidence),
            "production_eligible": False,
            "eligibility_blockers": list(evidence.eligibility_blockers),
        }
    except Exception as error:  # noqa: BLE001 -- serialize classified failures; never select a fallback.
        artifact = _failure_artifact(
            profile, as_of, error, requests=budget.requests, retries=budget.retries
        )
    finally:
        reset_retry_budget(token)
    _write_private_artifact(artifact_path, artifact)
    return artifact


@contextmanager
def _budget_unit(unit_id: str, cap: int, deadline: float) -> Generator[RetryBudget, None, None]:
    budget = current_retry_budget()
    if budget is None:
        raise RuntimeError("shadow acquisition unit requires an active source budget")
    with acquisition_unit(unit_id, cap, deadline):
        yield budget


def _profile_public_dict(profile: CandidateProfile) -> dict[str, object]:
    return {
        "candidate_id": profile.candidate_id,
        "source": profile.source.value,
        "source_contract_ids": list(profile.source_contract_ids),
        "source_contract_version": profile.source_contract_version,
        "code_version": profile.code_version,
        "endpoint_host": profile.endpoint_host,
        "adapter": profile.adapter.value if profile.adapter else None,
        "status": profile.status.value,
        "enabled_for_ingestion": profile.enabled_for_ingestion,
        "license_status": profile.license_status,
        "official_evidence_url": profile.official_evidence_url,
        "license_evidence_url": profile.license_evidence_url,
    }


def _safe_endpoint(url: str) -> str:
    parsed = httpx.URL(url)
    return f"{parsed.scheme}://{parsed.host}{parsed.path}"
