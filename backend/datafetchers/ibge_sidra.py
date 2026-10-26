from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, TypedDict, cast

import httpx
from pydantic import TypeAdapter, ValidationError

from db import store
from db.sidra_store import insert_sidra_observations, query_sidra_history
from ingestion_policy import ErrorCategory, RetryProfile, current_retry_budget
from provider_http import ProviderRequest, get_provider_response
from source_ingestion import REGISTRY, acquisition_unit, source_retry_scope

if TYPE_CHECKING:
    from candidate_acquisition import AcquiredBatch, CandidateProfile
    from candidate_eligibility import CandidateApproval
    from shadow_ingestion import CanonicalObservation

SIDRA_BASE_URL: Final = "https://apisidra.ibge.gov.br/values"
TIMEOUT_SECONDS: Final = 30.0
MAX_RESPONSE_BYTES: Final = 5 * 1024 * 1024
MAX_ATTEMPTS: Final = 3
_PERIOD_PATTERN: Final = re.compile(r"^\d{6}$")
_MISSING_VALUES: Final = frozenset({"", "-", "...", "X"})
Frequency = Literal["monthly", "moving_quarter"]
SidraRawRow = dict[str, str]
_JSON_ADAPTER: Final[TypeAdapter[list[SidraRawRow]]] = TypeAdapter(list[SidraRawRow])


@dataclass(frozen=True, slots=True)
class SidraDatasetSpec:
    key: str
    table_id: int
    variable_id: int
    classification_id: int | None
    category_id: int | None
    table_label: str
    variable_label: str
    classification_label: str | None
    category_label: str | None
    unit: str
    unit_code: str
    unit_label: str
    base_version: str | None
    frequency: Frequency
    territory_level: str = "N1"
    territory_code: str = "1"


class SidraObservation(TypedDict):
    dataset_key: str
    table_id: int
    variable_id: int
    classification_id: int | None
    category_id: int | None
    territory_level: str
    territory_code: str
    reference_period: str
    reference_label: str
    value: float
    unit: str
    base_version: str | None
    source_published_at: None
    first_seen_at: str
    frequency: Frequency


class SidraPayloadError(ValueError):
    error_category = ErrorCategory.INVALID_SCHEMA


class SidraRetryStateError(RuntimeError):
    pass


_SPECS: Final = (
    SidraDatasetSpec("pim_industria", 8888, 12607, 544, 129314, "Produção Física Industrial, por seções e atividades industriais", "PIMPF - Número-índice com ajuste sazonal (2022=100)", "Seções e atividades industriais (CNAE 2.0)", "1 Indústria geral", "index", "30", "Número-índice", "2022=100", "monthly"),
    SidraDatasetSpec("pmc_varejo_ampliado", 8881, 7170, 11046, 56736, "Índice e variação da receita nominal e do volume de vendas no comércio varejista ampliado (2022=100)", "PMC - Número-índice com ajuste sazonal (2022=100)", "Tipos de índice", "Índice de volume de vendas no comércio varejista ampliado", "index", "30", "Número-índice", "2022=100", "monthly"),
    SidraDatasetSpec("pms_servicos", 5906, 7168, 11046, 56726, "Índice e variação da receita nominal e do volume de serviços (2022=100)", "PMS - Número-índice com ajuste sazonal (2022=100)", "Tipos de índice", "Índice de volume de serviços", "index", "30", "Número-índice", "2022=100", "monthly"),
    SidraDatasetSpec("massa_rendimento_real", 6392, 6293, None, None, "Massa de rendimento mensal real e nominal", "Massa de rendimento mensal real das pessoas de 14 anos ou mais", None, None, "brl_million", "1550", "Milhões de Reais", "real_deflator_latest_published_moving_quarter", "moving_quarter"),
    SidraDatasetSpec("taxa_subutilizacao", 6441, 4118, None, None, "Taxa composta da subutilização da força de trabalho", "Taxa composta de subutilização da força de trabalho", None, None, "pct", "2", "Percentual", None, "moving_quarter"),
)

SIDRA_DATASETS: Final[Mapping[str, SidraDatasetSpec]] = MappingProxyType(
    {spec.key: spec for spec in _SPECS}
)


def build_sidra_url(spec: SidraDatasetSpec, period: str = "all") -> str:
    path = (
        f"{SIDRA_BASE_URL}/t/{spec.table_id}/n1/{spec.territory_code}"
        f"/v/{spec.variable_id}/p/{period}"
    )
    if spec.classification_id is not None and spec.category_id is not None:
        path += f"/c{spec.classification_id}/{spec.category_id}"
    return f"{path}?formato=json"


def _text(row: Mapping[str, str], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise SidraPayloadError(f"SIDRA field {key} must be text")
    return value


def _validate_header(header: Mapping[str, str], classified: bool) -> None:
    required = {"NC", "NN", "MC", "MN", "V", "D1C", "D1N", "D2C", "D2N", "D3C", "D3N"}
    if classified:
        required.update({"D4C", "D4N"})
    if not required.issubset(header):
        raise SidraPayloadError("SIDRA header is incomplete")
    if _text(header, "V") != "Valor":
        raise SidraPayloadError("SIDRA header does not identify values")


def _parse_value(value: str) -> float | None:
    if value in _MISSING_VALUES:
        return None
    try:
        parsed = float(value.replace(",", "."))
    except ValueError as exc:
        raise SidraPayloadError("SIDRA value is not numeric") from exc
    if not isfinite(parsed):
        raise SidraPayloadError("SIDRA value is not finite")
    return parsed


def _parse_row(
    spec: SidraDatasetSpec,
    row: Mapping[str, str],
    first_seen_at: str,
) -> SidraObservation | None:
    if _text(row, "NC") != "1" or _text(row, "D1C") != spec.territory_code:
        raise SidraPayloadError("SIDRA territory does not match Brazil N1")
    if _text(row, "NN") != "Brasil" or _text(row, "D1N") != "Brasil":
        raise SidraPayloadError("SIDRA territory label does not match Brazil")
    if _text(row, "D2C") != str(spec.variable_id):
        raise SidraPayloadError("SIDRA variable does not match the dataset spec")
    period = _text(row, "D3C")
    if _PERIOD_PATTERN.fullmatch(period) is None:
        raise SidraPayloadError("SIDRA reference period must use AAAAMM")
    try:
        datetime(int(period[:4]), int(period[4:]), 1, tzinfo=timezone.utc)
    except ValueError as exc:
        raise SidraPayloadError("SIDRA reference period must be a valid AAAAMM month") from exc
    period_label = _text(row, "D3N")
    if not period_label:
        raise SidraPayloadError("SIDRA reference period label is missing")
    if spec.category_id is not None and _text(row, "D4C") != str(spec.category_id):
        raise SidraPayloadError("SIDRA category does not match the dataset spec")
    raw_value = _text(row, "V")
    unit_code = _text(row, "MC")
    if not unit_code and raw_value == "...":
        return None
    if unit_code != spec.unit_code:
        raise SidraPayloadError("SIDRA unit does not match the dataset spec")
    value = _parse_value(raw_value)
    if value is None:
        return None
    return {
        "dataset_key": spec.key,
        "table_id": spec.table_id,
        "variable_id": spec.variable_id,
        "classification_id": spec.classification_id,
        "category_id": spec.category_id,
        "territory_level": spec.territory_level,
        "territory_code": spec.territory_code,
        "reference_period": period,
        "reference_label": period_label,
        "value": value,
        "unit": spec.unit,
        "base_version": spec.base_version,
        "source_published_at": None,
        "first_seen_at": first_seen_at,
        "frequency": spec.frequency,
    }


def parse_sidra_payload(
    spec: SidraDatasetSpec,
    content: bytes,
    *,
    first_seen_at: str,
) -> list[SidraObservation]:
    try:
        payload = _JSON_ADAPTER.validate_json(content)
    except ValidationError as exc:
        raise SidraPayloadError("SIDRA payload is not valid JSON") from exc
    if len(payload) < 2:
        raise SidraPayloadError("SIDRA payload must contain a header and observations")
    _validate_header(payload[0], spec.category_id is not None)
    observations = [
        observation
        for row in payload[1:]
        if (observation := _parse_row(spec, row, first_seen_at)) is not None
    ]
    if not observations:
        raise SidraPayloadError("SIDRA payload contains no usable observations")
    return observations


def fetch_sidra_dataset(
    spec: SidraDatasetSpec,
    *,
    request: ProviderRequest | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
) -> list[SidraObservation]:
    url = build_sidra_url(spec)
    unit = f"sidra:{spec.table_id}:{spec.variable_id}:{spec.frequency}"
    budget = current_retry_budget()
    unit_context = acquisition_unit(unit, MAX_ATTEMPTS, 90.0) if budget is not None else None
    if unit_context is not None:
        unit_context.__enter__()
    try:
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = get_provider_response(
                    url, request=request, timeout=TIMEOUT_SECONDS,
                    max_bytes=MAX_RESPONSE_BYTES, follow_redirects=True,
                ) if request is not None else get_provider_response(
                    url, timeout=TIMEOUT_SECONDS, max_bytes=MAX_RESPONSE_BYTES,
                    follow_redirects=True,
                )
                seen_at = datetime.now(timezone.utc).isoformat()
                return parse_sidra_payload(spec, response.content, first_seen_at=seen_at)
            except httpx.HTTPStatusError as exc:
                retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
                if not retryable or attempt + 1 == MAX_ATTEMPTS:
                    raise
            except httpx.TransportError:
                if attempt + 1 == MAX_ATTEMPTS:
                    raise
            sleep((2**attempt) + jitter())
    finally:
        if unit_context is not None:
            unit_context.__exit__(None, None, None)
    raise SidraRetryStateError("unreachable SIDRA retry state")


def refresh_sidra_data() -> dict[str, list[SidraObservation]]:
    refreshed: dict[str, list[SidraObservation]] = {}
    with source_retry_scope("ibge_sidra", "primary", RetryProfile("ibge_sidra", 15, 240.0, 30.0)):
        for key, spec in SIDRA_DATASETS.items():
            try:
                def fetch_and_persist(dataset_spec: SidraDatasetSpec = spec) -> list[SidraObservation]:
                    from candidate_acquisition import DEFAULT_CANDIDATE_ACQUISITION

                    profile, approval, _blocker = DEFAULT_CANDIDATE_ACQUISITION.resolve(
                        f"ibge_sidra:v3:{dataset_spec.key}", f"sidra:{dataset_spec.key}"
                    )
                    if profile is not None and approval is not None:
                        return _fetch_qualified_sidra_dataset(
                            profile, approval, dataset_spec
                        )
                    observations = fetch_sidra_dataset(dataset_spec)
                    insert_sidra_observations(observations)
                    return observations

                observations = cast(list[SidraObservation], REGISTRY["ibge_sidra"].fetch(
                    indicator=key,
                    qualifiers={"table_id": spec.table_id, "variable_id": spec.variable_id,
                                "frequency": spec.frequency},
                    operation=fetch_and_persist,
                    network_expected=True,
                ))
                refreshed[key] = observations
            except (httpx.HTTPError, SidraPayloadError, ValueError):
                refreshed[key] = []
    return refreshed


def _fetch_qualified_sidra_dataset(
    profile: CandidateProfile,
    approval: CandidateApproval,
    spec: SidraDatasetSpec,
) -> list[SidraObservation]:
    """Acquire a whole SIDRA dataset from one route, then persist it atomically."""
    from candidate_acquisition import (
        DEFAULT_CANDIDATE_ACQUISITION,
        CandidateSemanticConflict,
        SelectionResult,
        build_origin_rows,
        commit_receipt,
        commit_receipt_matches,
        select_and_persist,
    )
    from provider_hosts import require_allowed_provider_url
    from shadow_ingestion import parse_sidra_v3

    as_of = datetime.now(timezone.utc).date()
    candidate_url = require_allowed_provider_url(
        f"https://{profile.endpoint_host}"
        f"{(profile.endpoint_path_template or '').format(
            table_id=spec.table_id, period_range='all', variable_id=spec.variable_id
        )}"
    )
    candidate_params = {"localidades": f"N1[{spec.territory_code}]"}
    if spec.classification_id is not None and spec.category_id is not None:
        candidate_params["classificacao"] = f"{spec.classification_id}[{spec.category_id}]"
    primary_url = require_allowed_provider_url(build_sidra_url(spec, period="all"))

    def primary_acquire(timeout: float) -> AcquiredBatch:
        response = get_provider_response(
            primary_url,
            timeout=timeout,
            max_bytes=MAX_RESPONSE_BYTES,
            follow_redirects=True,
        )
        captured_at = datetime.now(timezone.utc).isoformat()
        observations = parse_sidra_payload(
            spec, response.content, first_seen_at=captured_at
        )
        filtered = _sidra_as_of(observations, as_of)
        return _sidra_acquired_batch(filtered, sha256(response.content).hexdigest(), captured_at)

    def candidate_acquire(timeout: float) -> AcquiredBatch:
        response = get_provider_response(
            candidate_url,
            timeout=timeout,
            max_bytes=profile.max_bytes,
            params=candidate_params,
            follow_redirects=True,
        )
        captured_at = datetime.now(timezone.utc).isoformat()
        canonical = parse_sidra_v3(profile, spec, response.content, as_of=as_of)
        observations = _sidra_candidate_observations(canonical, spec, as_of, captured_at)
        return _sidra_acquired_batch(
            observations, sha256(response.content).hexdigest(), captured_at
        )

    existing = query_sidra_history(spec.key)
    existing_periods = {str(row["reference_period"]) for row in existing}
    existing_values = {
        str(row["reference_period"]): float(str(row["value"])) for row in existing
    }

    def stored_latest() -> str | None:
        return max((f"{period[:4]}-{period[4:]}-01" for period in existing_periods), default=None)

    def persist(selection: SelectionResult) -> None:
        observations = selection.observations
        periods = {str(row["reference_period"]) for row in observations}
        # The v3 endpoint must reproduce every previously stored period when the
        # producer's `all` scope is selected; a recent-only response is incomplete.
        if not existing_periods.issubset(periods):
            raise SidraPayloadError("SIDRA candidate does not cover the stored complete cohort")
        if selection.route == "candidate":
            for row in observations:
                period = str(row["reference_period"])
                old_value = existing_values.get(period)
                if old_value is not None and old_value != float(str(row["value"])):
                    raise CandidateSemanticConflict(
                        "candidate changed a stored SIDRA value without publication ordering"
                    )
        origins = build_origin_rows(
            extraction_id=selection.extraction_id,
            source="ibge_sidra",
            contract_id=f"sidra:{spec.key}",
            route=selection.route,
            candidate_id=selection.candidate_id,
            source_payload_sha256=selection.payload_sha256,
            observations=observations,
            identity_fields=(
                "table_id", "variable_id", "territory_level", "territory_code",
                "classification_id", "category_id", "reference_period", "unit", "base_version",
            ),
            reference_field="reference_period",
            value_field="value",
            reference_date=lambda value: f"{str(value)[:4]}-{str(value)[4:]}-01",
            captured_at=selection.captured_at,
            contract_version=selection.contract_version,
            profile_semantic_sha256=selection.profile_semantic_sha256,
            parser_semantic_sha256=selection.parser_semantic_sha256,
            proof_sha256=selection.proof_sha256,
        )
        receipt = commit_receipt(
            selection.extraction_id,
            route=selection.route,
            candidate_id=selection.candidate_id,
            observation_count=len(observations),
            source_payload_sha256=selection.payload_sha256,
        )
        prior_receipt = store.source_acquisition_commit(selection.extraction_id)
        if prior_receipt is not None:
            if not commit_receipt_matches(selection):
                raise RuntimeError("source operation id has a conflicting commit receipt")
            return
        insert_sidra_observations(
            observations,
            first_seen_at=selection.captured_at,
            origin_rows=origins,
            commit_receipt=receipt,
        )

    result = select_and_persist(
        profile=profile,
        approval=approval,
        indicator=spec.key,
        contract_id=f"sidra:{spec.key}",
        acquisition_unit_id=f"sidra:{spec.table_id}:{spec.variable_id}:{spec.frequency}",
        primary_acquire=primary_acquire,
        candidate_acquire=candidate_acquire,
        stored_latest=stored_latest,
        persist=persist,
        unit_deadline_seconds=profile.deadline_seconds,
        service=DEFAULT_CANDIDATE_ACQUISITION,
    )
    return cast(list[SidraObservation], list(result.observations))


def _sidra_as_of(
    observations: list[SidraObservation], as_of: date
) -> list[SidraObservation]:
    cutoff = f"{as_of.year:04d}{as_of.month:02d}"
    if any(row["reference_period"] > cutoff for row in observations):
        raise SidraPayloadError("SIDRA primary contains a future reference period")
    return observations


def _sidra_candidate_observations(
    canonical: Sequence[CanonicalObservation],
    spec: SidraDatasetSpec,
    as_of: date,
    captured_at: str,
) -> list[SidraObservation]:
    existing_labels = {
        str(row["reference_period"]): str(row["reference_label"])
        for row in query_sidra_history(spec.key)
    }
    result: list[SidraObservation] = []
    for item in canonical:
        period = item.reference_date[:4] + item.reference_date[5:7]
        if period > f"{as_of.year:04d}{as_of.month:02d}":
            raise SidraPayloadError("SIDRA candidate contains a future reference period")
        values = dict(item.values)
        result.append({
            "dataset_key": spec.key,
            "table_id": spec.table_id,
            "variable_id": spec.variable_id,
            "classification_id": spec.classification_id,
            "category_id": spec.category_id,
            "territory_level": spec.territory_level,
            "territory_code": spec.territory_code,
            "reference_period": period,
            "reference_label": existing_labels.get(period, _sidra_reference_label(period, spec.frequency)),
            "value": values["value"],
            "unit": spec.unit,
            "base_version": spec.base_version,
            "source_published_at": None,
            "first_seen_at": captured_at,
            "frequency": spec.frequency,
        })
    if not result:
        raise SidraPayloadError("SIDRA candidate contains no observations")
    return result


def _sidra_reference_label(period: str, frequency: Frequency) -> str:
    month = int(period[4:])
    year = period[:4]
    full_names = (
        "janeiro", "fevereiro", "março", "abril", "maio", "junho",
        "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
    )
    abbreviations = (
        "jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez",
    )
    if frequency == "monthly":
        return f"{full_names[month - 1]} {year}"
    quarter_months = ((month - 3) % 12, (month - 2) % 12, month - 1)
    return f"{'-'.join(abbreviations[index] for index in quarter_months)} {year}"


def _sidra_acquired_batch(
    observations: Sequence[SidraObservation], payload_sha256: str, captured_at: str
) -> AcquiredBatch:
    from candidate_acquisition import AcquiredBatch

    periods = tuple(
        f"{row['reference_period'][:4]}-{row['reference_period'][4:]}-01"
        for row in observations
    )
    if not periods:
        raise SidraPayloadError("SIDRA source returned an empty cohort")
    return AcquiredBatch(tuple(observations), payload_sha256, periods, captured_at)


def sidra_acquired_batch(
    observations: Sequence[SidraObservation], payload_sha256: str, captured_at: str
) -> AcquiredBatch:
    """Construct the canonical producer batch from validated SIDRA observations."""
    return _sidra_acquired_batch(observations, payload_sha256, captured_at)
