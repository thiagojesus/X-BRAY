"""Immutable, primary-disabled candidate inventory for Phase 3 shadow work.

The registry is metadata only: resolving a profile never constructs a client or
performs network or database I/O. Only routes with an implemented strict decoder
have a shadow adapter; descriptors without one fail closed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Final

from datafetchers.ibge_sidra import SIDRA_DATASETS
from source_contracts import (
    CONTRACT_BY_ID,
    FOCUS_ANNUAL_CONTRACTS,
    FOCUS_ROLLING_CONTRACT,
    SCHEMA_VERSION,
    SGS_CONTRACTS,
    SIDRA_CONTRACTS,
    Source,
)

CANDIDATE_CODE_VERSION: Final = "shadow-adapters-v1"


class CandidateStatus(StrEnum):
    UNVERIFIED = "unverified"
    SHADOW = "shadow"
    ELIGIBLE = "eligible"
    DISABLED = "disabled"


class ShadowAdapter(StrEnum):
    SGS_CSV = "sgs_csv_v1"
    SIDRA_V3 = "sidra_v3_v1"


@dataclass(frozen=True, slots=True)
class CandidateProfile:
    candidate_id: str
    source: Source
    source_contract_ids: tuple[str, ...]
    source_contract_version: str
    code_version: str
    endpoint_host: str | None
    endpoint_path_template: str | None
    fixed_parameters: tuple[tuple[str, str], ...]
    adapter: ShadowAdapter | None
    value_unit: str
    frequency: str
    reference_calendar: str
    field_coverage: tuple[str, ...]
    priority: int
    max_bytes: int
    deadline_seconds: float
    request_cap: int
    quota_scope: str
    failure_domain: str
    official_evidence_url: str | None
    license_evidence_url: str | None
    license_status: str
    status: CandidateStatus
    enabled_for_ingestion: bool
    disabled_reason: str | None
    approved_proof_sha256: str | None = None
    code_reviewed: bool = False

    def profile_sha256(self) -> str:
        payload = asdict(self)
        payload["source"] = self.source.value
        payload["status"] = self.status.value
        payload["adapter"] = self.adapter.value if self.adapter is not None else None
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def ingestion_eligible(self) -> bool:
        """Eligibility is a code-reviewed state, never a CLI/artifact flag."""
        return (
            self.status is CandidateStatus.ELIGIBLE
            and self.enabled_for_ingestion
            and self.code_reviewed
            and self.approved_proof_sha256 is not None
            and self.license_status == "verified"
        )


def _profile(
    *,
    candidate_id: str,
    source: Source,
    contract_ids: tuple[str, ...],
    adapter: ShadowAdapter | None,
    host: str | None,
    endpoint: str | None,
    fixed_parameters: tuple[tuple[str, str], ...],
    priority: int,
    max_bytes: int,
    deadline_seconds: float,
    request_cap: int,
    official_url: str | None,
    status: CandidateStatus,
    disabled_reason: str | None,
    failure_domain: str,
) -> CandidateProfile:
    contracts = tuple(CONTRACT_BY_ID[item] for item in contract_ids)
    fields = tuple(dict.fromkeys(
        field
        for contract in contracts
        for field in (*contract.identity, *contract.value_fields, "unit", "frequency")
    ))
    return CandidateProfile(
        candidate_id=candidate_id,
        source=source,
        source_contract_ids=contract_ids,
        source_contract_version=SCHEMA_VERSION,
        code_version=CANDIDATE_CODE_VERSION,
        endpoint_host=host,
        endpoint_path_template=endpoint,
        fixed_parameters=fixed_parameters,
        adapter=adapter,
        value_unit=contracts[0].unit if contracts else "unknown",
        frequency=contracts[0].frequency if contracts else "unknown",
        reference_calendar="UTC source reference dates",
        field_coverage=fields,
        priority=priority,
        max_bytes=max_bytes,
        deadline_seconds=deadline_seconds,
        request_cap=request_cap,
        quota_scope=source.value,
        failure_domain=f"{source.value}:primary-publisher",
        official_evidence_url=official_url,
        license_evidence_url=None,
        license_status="unverified",
        status=status,
        enabled_for_ingestion=False,
        disabled_reason=disabled_reason,
        approved_proof_sha256=None,
        code_reviewed=False,
    )


def _build_registry() -> Mapping[str, CandidateProfile]:
    profiles: dict[str, CandidateProfile] = {}
    for contract in SGS_CONTRACTS:
        code = contract.contract_id.removeprefix("sgs:")
        candidate_id = f"bcb_sgs:csv:{code}"
        profiles[candidate_id] = _profile(
            candidate_id=candidate_id,
            source=Source.SGS,
            contract_ids=(contract.contract_id,),
            adapter=ShadowAdapter.SGS_CSV,
            host="api.bcb.gov.br",
            endpoint="/dados/serie/bcdata.sgs.{series_code}/dados",
            fixed_parameters=(("formato", "csv"),),
            priority=20,
            max_bytes=1024 * 1024,
            deadline_seconds=20.0,
            request_cap=3,
            official_url="https://api.bcb.gov.br/",
            status=CandidateStatus.SHADOW,
            disabled_reason=None,
            failure_domain="bcb_sgs:api.bcb.gov.br",
        )
    for contract in SIDRA_CONTRACTS:
        dataset_key = contract.contract_id.removeprefix("sidra:")
        spec = SIDRA_DATASETS[dataset_key]
        candidate_id = f"ibge_sidra:v3:{dataset_key}"
        v3_parameters = (("localidades", f"N1[{spec.territory_code}]"),)
        if spec.classification_id is not None and spec.category_id is not None:
            v3_parameters += (
                ("classificacao", f"{spec.classification_id}[{spec.category_id}]"),
            )
        profiles[candidate_id] = _profile(
            candidate_id=candidate_id,
            source=Source.SIDRA,
            contract_ids=(contract.contract_id,),
            adapter=ShadowAdapter.SIDRA_V3,
            host="servicodados.ibge.gov.br",
            endpoint="/api/v3/agregados/{table_id}/periodos/{period_range}/variaveis/{variable_id}",
            fixed_parameters=v3_parameters,
            priority=20,
            max_bytes=2 * 1024 * 1024,
            deadline_seconds=30.0,
            request_cap=3,
            official_url="https://servicodados.ibge.gov.br/api/docs/agregados?versao=3",
            status=CandidateStatus.SHADOW,
            disabled_reason=None,
            failure_domain="ibge_sidra:servicodados.ibge.gov.br",
        )

    disabled: tuple[tuple[str, Source, tuple[str, ...], str], ...] = (
        ("focus:alternate:unverified", Source.FOCUS,
         tuple(contract.contract_id for contract in (*FOCUS_ANNUAL_CONTRACTS, FOCUS_ROLLING_CONTRACT)),
         "No official free alternative with verified method, units and cohort coverage."),
        ("b3:alternate:unverified", Source.B3, ("b3:DI1-adjusted-curve-v1",),
         "No distinct official equivalent route verified; BVBG187A is part of the primary SPRD archive."),
        ("anbima:txt:unverified", Source.ANBIMA, ("anbima:ima-b-workbook-v1",),
         "Official TXT route is not yet proven to cover all eight consumed workbook subdatasets or licensing."),
        ("tesouro:alternate:unverified", Source.TREASURY, ("tesouro:daily-offer-quotes-v1",),
         "No distinct official observations route verified; metadata/ETag is not a quote cohort."),
    )
    for candidate_id, source, contracts, reason in disabled:
        profiles[candidate_id] = _profile(
            candidate_id=candidate_id,
            source=source,
            contract_ids=contracts,
            adapter=None,
            host=None,
            endpoint=None,
            fixed_parameters=(),
            priority=100,
            max_bytes=0,
            deadline_seconds=0.0,
            request_cap=0,
            official_url=None,
            status=CandidateStatus.DISABLED,
            disabled_reason=reason,
            failure_domain=f"{source.value}:unverified-route",
        )
    return MappingProxyType(profiles)


CANDIDATE_REGISTRY: Final[Mapping[str, CandidateProfile]] = _build_registry()


def get_candidate_profile(candidate_id: str) -> CandidateProfile:
    """Resolve only an exact catalog key; never accept a caller-supplied URL."""
    try:
        return CANDIDATE_REGISTRY[candidate_id]
    except KeyError as exc:
        raise ValueError("unknown source candidate id") from exc
