"""Versioned, read-only acquisition contracts for the currently supported sources.

These contracts describe the smallest coherent unit which a future alternative
source would have to reproduce. They do not select providers or initiate I/O.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from datafetchers.ibge_sidra import SIDRA_DATASETS
from indicator_catalog import INDICATOR_CATALOG

SCHEMA_VERSION: Final = "acquisition-contract-v1"
SGS_PIB_SCHEMA_VERSION: Final = "acquisition-contract-v2"


class Source(StrEnum):
    SGS = "bcb_sgs"
    FOCUS = "focus"
    SIDRA = "ibge_sidra"
    B3 = "b3"
    ANBIMA = "anbima"
    TREASURY = "tesouro"


@dataclass(frozen=True, slots=True)
class AcquisitionContract:
    """Identity, value and time dimensions of one atomic source delivery."""

    schema_version: str
    source: Source
    contract_id: str
    identity: tuple[str, ...]
    value_fields: tuple[str, ...]
    unit: str
    frequency: str
    atomic_group: tuple[str, ...]
    reference_date_field: str
    source_published_at_field: str | None = None
    capture_time_is_publication_time: bool = False
    qualifiers: tuple[tuple[str, str], ...] = ()


def _contract(
    source: Source,
    contract_id: str,
    identity: tuple[str, ...],
    value_fields: tuple[str, ...],
    unit: str,
    frequency: str,
    atomic_group: tuple[str, ...],
    reference_date_field: str,
    *,
    qualifiers: tuple[tuple[str, str], ...] = (),
) -> AcquisitionContract:
    return AcquisitionContract(
        schema_version=SCHEMA_VERSION,
        source=source,
        contract_id=contract_id,
        identity=identity,
        value_fields=value_fields,
        unit=unit,
        frequency=frequency,
        atomic_group=atomic_group,
        reference_date_field=reference_date_field,
        qualifiers=qualifiers,
    )


SGS_CONTRACTS: Final[tuple[AcquisitionContract, ...]] = tuple(
    _contract(
        Source.SGS,
        f"sgs:{spec.provider_code}",
        (("series_code", "period_start", "period_end")
         if int(spec.provider_code) == 226 else ("series_code", "reference_date")),
        ("value",),
        "pct_period" if int(spec.provider_code) == 226 else spec.unit.value,
        "monthly_period" if int(spec.provider_code) == 226 else spec.frequency.value,
        ("series_code",),
        "period_start" if int(spec.provider_code) == 226 else "data",
        qualifiers=(
            (("indicator_key", str(spec.key)),
             ("identity_rule", "full period start and end; never point-date"))
            if int(spec.provider_code) == 226
            else (("indicator_key", str(spec.key)),)
        ),
    )
    for spec in INDICATOR_CATALOG.values()
    if int(spec.provider_code) != 1207
) + (
    AcquisitionContract(
        schema_version=SGS_PIB_SCHEMA_VERSION,
        source=Source.SGS,
        contract_id="sgs:1207",
        identity=("series_code", "reference_date"),
        value_fields=("value",),
        unit="brl",
        frequency="annual",
        atomic_group=("series_code",),
        reference_date_field="data",
        qualifiers=(("indicator_key", "pib"), ("economic_concept", "nominal_gdp_current_brl")),
    ),
)

LEGACY_SGS_PIB_CONTRACT: Final = AcquisitionContract(
    schema_version=SCHEMA_VERSION,
    source=Source.SGS,
    contract_id="sgs:1207",
    identity=("series_code", "reference_date"),
    value_fields=("value",),
    unit="brl_million",
    frequency="quarterly",
    atomic_group=("series_code",),
    reference_date_field="data",
    qualifiers=(("indicator_key", "pib"),),
)


def resolve_sgs_contract(code: int, version: str) -> AcquisitionContract | None:
    """Resolve current and historically frozen contract semantics for one SGS code."""
    if code == 1207 and version == SCHEMA_VERSION:
        return LEGACY_SGS_PIB_CONTRACT
    return next(
        (item for item in SGS_CONTRACTS
         if item.contract_id == f"sgs:{code}" and item.schema_version == version),
        None,
    )


def is_current_sgs_contract(code: int, version: str) -> bool:
    """Whether a proof version attests the current source meaning, not just readable history."""
    return any(
        item.contract_id == f"sgs:{code}" and item.schema_version == version
        for item in SGS_CONTRACTS
    )


def sgs_contract_semantics_match(code: int, version: str, unit: str, frequency: str) -> bool:
    """Check persisted unit/frequency against the exact versioned contract."""
    contract = resolve_sgs_contract(code, version)
    if contract is None or contract.unit != unit:
        return False
    expected_frequency = "monthly" if code == 226 and contract.frequency == "monthly_period" else contract.frequency
    return frequency == expected_frequency


def current_sgs_unit_metadata_matches(
    code: int,
    contract_id: str,
    version: str,
    unit: str,
    frequency: str,
    identity_kind: str,
    method: str,
) -> bool:
    """Validate all unit metadata against the exact current SGS semantic contract."""
    if not is_current_sgs_contract(code, version) or contract_id != f"sgs:{code}":
        return False
    contract = resolve_sgs_contract(code, version)
    if contract is None or not sgs_contract_semantics_match(code, version, unit, frequency):
        return False
    full_period = contract.identity == ("series_code", "period_start", "period_end")
    expected_identity = "full_period" if full_period else "point"
    expected_method = "full_period" if full_period else "date_range"
    return identity_kind == expected_identity and method == expected_method

TR_PERIOD_CONTRACT: Final = next(
    contract for contract in SGS_CONTRACTS if contract.contract_id == "sgs:226"
)

SGS_POINT_IDENTITY_KIND: Final = "point"
SGS_FULL_PERIOD_IDENTITY_KIND: Final = "full_period"


def sgs_window_contract_metadata(
    code: int,
    start_date: str,
    end_date: str,
) -> dict[str, str | int]:
    """Return explicit code-owned contract identity for one SGS request scope."""
    contract = next(
        (item for item in SGS_CONTRACTS if item.contract_id == f"sgs:{code}"),
        None,
    )
    if contract is None:
        raise ValueError(f"unknown SGS contract code: {code}")
    if contract.identity == ("series_code", "period_start", "period_end"):
        identity_kind = SGS_FULL_PERIOD_IDENTITY_KIND
    elif contract.identity == ("series_code", "reference_date"):
        identity_kind = SGS_POINT_IDENTITY_KIND
    else:
        raise ValueError(f"unsupported SGS identity for code: {code}")
    return {
        "series_code": code,
        "start_date": start_date,
        "end_date": end_date,
        "contract_version": contract.schema_version,
        "identity_kind": identity_kind,
        "unit": contract.unit,
    }

FOCUS_ANNUAL_CONTRACTS: Final[tuple[AcquisitionContract, ...]] = tuple(
    _contract(
        Source.FOCUS,
        f"focus:annual:{indicator}",
        ("indicator", "target_year", "baseCalculo", "DataReferencia"),
        ("Mediana",),
        "indicator_specific_native_unit",
        "annual_target",
        ("indicator", "DataReferencia", "baseCalculo"),
        "DataReferencia",
        qualifiers=(("indicator", indicator), ("bases", "0,1"), ("horizon_offsets", "0..4"),
                    ("Suavizada", "absent")),
    )
    for indicator in ("IPCA", "Selic", "PIB Total", "Câmbio", "IGP-M")
)
FOCUS_ROLLING_CONTRACT: Final = _contract(
    Source.FOCUS,
    "focus:rolling:IPCA_12M_S_BASE0",
    ("indicator", "DataReferencia", "baseCalculo", "Suavizada"),
    ("Mediana",),
    "pct_annualized",
    "rolling_12_months",
    ("indicator", "DataReferencia", "baseCalculo", "Suavizada"),
    "DataReferencia",
    qualifiers=(("indicator", "IPCA"), ("baseCalculo", "0"), ("Suavizada", "S")),
)

SIDRA_CONTRACTS: Final[tuple[AcquisitionContract, ...]] = tuple(
    _contract(
        Source.SIDRA,
        f"sidra:{spec.key}",
        ("table_id", "variable_id", "territory_level", "territory_code",
         "classification_id", "category_id", "reference_period", "unit", "base_version"),
        ("value",),
        spec.unit,
        spec.frequency,
        ("dataset_key",),
        "reference_period",
        qualifiers=(("dataset_key", spec.key), ("geography", "N1:1")),
    )
    for spec in SIDRA_DATASETS.values()
)

B3_DI_CONTRACT: Final = _contract(
    Source.B3,
    "b3:DI1-adjusted-curve-v1",
    ("trade_date", "contract_symbol", "maturity_date", "quote_type", "adjustment_method"),
    ("adjusted_rate_252_annual",),
    "pct_annualized_252",
    "market_by_trade_date",
    ("trade_date", "complete_daily_DI_curve"),
    "trade_date",
    qualifiers=(("contract_prefix", "DI1"), ("adjustment_method", "adjusted quote")),
)

ANBIMA_IMAB_CONTRACT: Final = _contract(
    Source.ANBIMA,
    "anbima:ima-b-workbook-v1",
    ("worksheet", "reference_date", "metric"),
    ("metric_value",),
    "metric_specific_native_unit",
    "daily_market",
    ("worksheet_cohort", "reference_date", "all_consumed_IMA-B_metrics"),
    "reference_date",
    qualifiers=(("index_cohort", "IMA-B"), ("alternate_TXT_equivalence", "unproven")),
)

TREASURY_CONTRACT: Final = _contract(
    Source.TREASURY,
    "tesouro:daily-offer-quotes-v1",
    ("data_base", "title_type", "title_name", "maturity_date", "quote_side"),
    ("purchase_price", "sale_price", "purchase_rate", "sale_rate"),
    "BRL_price_and_percent_rate_by_side",
    "daily_offer_cohort",
    ("data_base", "all_active_titles_and_quote_sides"),
    "data_base",
    qualifiers=(("publication_time", "unknown unless explicitly supplied"),
                ("settlement_basis", "source-defined; do not mix D+0/D+1")),
)

CONTRACTS: Final[tuple[AcquisitionContract, ...]] = (
    *SGS_CONTRACTS,
    *FOCUS_ANNUAL_CONTRACTS,
    FOCUS_ROLLING_CONTRACT,
    *SIDRA_CONTRACTS,
    B3_DI_CONTRACT,
    ANBIMA_IMAB_CONTRACT,
    TREASURY_CONTRACT,
)
CONTRACT_BY_ID: Final[Mapping[str, AcquisitionContract]] = MappingProxyType(
    {contract.contract_id: contract for contract in CONTRACTS}
)
