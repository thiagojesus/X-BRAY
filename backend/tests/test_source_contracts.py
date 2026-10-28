from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from datafetchers.focus import FOCUS_12M_KEY, INDICATORS
from datafetchers.ibge_sidra import SIDRA_DATASETS
from source_audit.catalog import SGS_CODES
from source_contracts import (
    ANBIMA_IMAB_CONTRACT,
    B3_DI_CONTRACT,
    CONTRACT_BY_ID,
    CONTRACTS,
    FOCUS_ANNUAL_CONTRACTS,
    FOCUS_ROLLING_CONTRACT,
    SCHEMA_VERSION,
    SGS_CONTRACTS,
    SGS_FULL_PERIOD_IDENTITY_KIND,
    SGS_POINT_IDENTITY_KIND,
    SIDRA_CONTRACTS,
    TR_PERIOD_CONTRACT,
    TREASURY_CONTRACT,
    Source,
    sgs_window_contract_metadata,
)
from source_ingestion import REGISTRY


def test_contract_inventory_binds_current_producer_catalogs_without_claiming_fallbacks() -> None:
    assert len(SGS_CONTRACTS) == len(SGS_CODES) == 64
    assert {int(contract.contract_id.split(":")[1]) for contract in SGS_CONTRACTS} == set(SGS_CODES)
    assert len(SIDRA_CONTRACTS) == len(SIDRA_DATASETS) == 5
    assert len(FOCUS_ANNUAL_CONTRACTS) == len(INDICATORS) == 5
    assert len(CONTRACT_BY_ID) == len(CONTRACTS)
    assert all(contract.schema_version == SCHEMA_VERSION for contract in CONTRACTS if contract.contract_id != "sgs:1207")
    pib = next(contract for contract in SGS_CONTRACTS if contract.contract_id == "sgs:1207")
    assert (pib.schema_version, pib.unit, pib.frequency) == ("acquisition-contract-v2", "brl", "annual")
    from source_contracts import resolve_sgs_contract
    legacy = resolve_sgs_contract(1207, SCHEMA_VERSION)
    assert legacy is not None and (legacy.unit, legacy.frequency) == ("brl_million", "quarterly")
    from source_contracts import sgs_contract_semantics_match
    assert sgs_contract_semantics_match(1207, "acquisition-contract-v2", "brl", "annual")
    assert not sgs_contract_semantics_match(
        1207, "acquisition-contract-v2", "brl_million", "quarterly"
    )
    assert not sgs_contract_semantics_match(432, "acquisition-contract-v2", "pct_annual", "daily")
    assert all(not hasattr(contract, "fallback_source") for contract in CONTRACTS)
    assert all(contract.source in set(Source) for contract in CONTRACTS)
    assert "polymarket" not in {contract.source.value for contract in CONTRACTS}
    assert set(REGISTRY) == {source.value for source in Source}


def test_tr_contract_requires_full_period_identity_not_point_date() -> None:
    assert TR_PERIOD_CONTRACT.contract_id == "sgs:226"
    assert TR_PERIOD_CONTRACT.identity == ("series_code", "period_start", "period_end")
    assert TR_PERIOD_CONTRACT.reference_date_field == "period_start"
    assert TR_PERIOD_CONTRACT.value_fields == ("value",)

    first = ("226", "2025-01-01", "2025-01-31", 0.1)
    different_period = ("226", "2025-01-01", "2025-02-01", 0.2)
    assert first[:3] != different_period[:3]
    assert not any(contract.contract_id == "sgs:7811" for contract in SGS_CONTRACTS)


def test_sgs_window_metadata_uses_canonical_point_and_full_period_contracts() -> None:
    point = sgs_window_contract_metadata(432, "01/01/2025", "31/12/2025")
    period = sgs_window_contract_metadata(226, "01/01/2025", "31/12/2025")
    pib = sgs_window_contract_metadata(1207, "01/01/2020", "28/10/2026")

    assert point == {
        "series_code": 432,
        "start_date": "01/01/2025",
        "end_date": "31/12/2025",
        "contract_version": SCHEMA_VERSION,
        "identity_kind": SGS_POINT_IDENTITY_KIND,
        "unit": next(item.unit for item in SGS_CONTRACTS if item.contract_id == "sgs:432"),
    }
    assert period["identity_kind"] == SGS_FULL_PERIOD_IDENTITY_KIND
    assert period["unit"] == "pct_period"
    assert (pib["contract_version"], pib["unit"]) == ("acquisition-contract-v2", "brl")


def test_sgs_window_metadata_rejects_unknown_and_noninteger_codes() -> None:
    with pytest.raises(ValueError, match="unknown SGS contract code"):
        sgs_window_contract_metadata(7811, "01/01/2025", "31/12/2025")
    with pytest.raises(ValueError, match="unknown SGS contract code"):
        sgs_window_contract_metadata(432.0, "01/01/2025", "31/12/2025")  # type: ignore[arg-type]


def test_sgs_window_metadata_rejects_unsupported_catalog_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    import source_contracts

    unsupported = replace(SGS_CONTRACTS[0], identity=("series_code", "unexpected"))
    monkeypatch.setattr(source_contracts, "SGS_CONTRACTS", (unsupported,))
    code = int(unsupported.contract_id.split(":")[1])

    with pytest.raises(ValueError, match="unsupported SGS identity"):
        source_contracts.sgs_window_contract_metadata(code, "01/01/2025", "31/12/2025")


def test_focus_family_and_rolling_contracts_preserve_distinct_identity_and_cache_groups() -> None:
    assert {dict(contract.qualifiers)["indicator"] for contract in FOCUS_ANNUAL_CONTRACTS} == {
        "IPCA", "Selic", "PIB Total", "Câmbio", "IGP-M"
    }
    for contract in FOCUS_ANNUAL_CONTRACTS:
        assert contract.identity == ("indicator", "target_year", "baseCalculo", "DataReferencia")
        assert dict(contract.qualifiers)["horizon_offsets"] == "0..4"
        assert dict(contract.qualifiers)["Suavizada"] == "absent"
        assert contract.atomic_group == ("indicator", "DataReferencia", "baseCalculo")
    assert FOCUS_ROLLING_CONTRACT.contract_id.endswith(FOCUS_12M_KEY)
    assert "Suavizada" in FOCUS_ROLLING_CONTRACT.identity
    assert dict(FOCUS_ROLLING_CONTRACT.qualifiers)["Suavizada"] == "S"
    assert all(contract.atomic_group[0] != "all_focus_families" for contract in FOCUS_ANNUAL_CONTRACTS)


def test_sidra_b3_anbima_and_treasury_contracts_retain_complete_cohorts() -> None:
    by_id = {contract.contract_id: contract for contract in CONTRACTS}
    for key, spec in SIDRA_DATASETS.items():
        contract = by_id[f"sidra:{key}"]
        assert dict(contract.qualifiers)["geography"] == "N1:1"
        assert contract.unit == spec.unit
        assert contract.frequency == spec.frequency
        assert "reference_period" in contract.identity
        assert "category_id" in contract.identity
    assert B3_DI_CONTRACT.atomic_group[-1] == "complete_daily_DI_curve"
    assert {"trade_date", "contract_symbol", "maturity_date", "quote_type", "adjustment_method"}.issubset(B3_DI_CONTRACT.identity)
    assert dict(ANBIMA_IMAB_CONTRACT.qualifiers)["alternate_TXT_equivalence"] == "unproven"
    assert "worksheet_cohort" in ANBIMA_IMAB_CONTRACT.atomic_group
    assert {"data_base", "title_type", "title_name", "maturity_date", "quote_side"}.issubset(TREASURY_CONTRACT.identity)
    assert "all_active_titles_and_quote_sides" in TREASURY_CONTRACT.atomic_group
    assert "unknown unless explicitly supplied" in dict(TREASURY_CONTRACT.qualifiers)["publication_time"]


def test_contracts_are_immutable_and_registry_is_read_only() -> None:
    with pytest.raises(FrozenInstanceError):
        TR_PERIOD_CONTRACT.unit = "point"  # type: ignore[misc]
    with pytest.raises(TypeError):
        CONTRACT_BY_ID["made-up"] = TR_PERIOD_CONTRACT  # type: ignore[index]


def test_existing_focus_contract_characterization_stays_bound_to_real_entrypoint() -> None:
    # The PG17 integration test exercises this producer through storage, cache and
    # source_extractions ledger. This assertion ties its existing identities to
    # the new contract inventory without coupling the audit parser to the producer.
    from datafetchers import focus

    assert focus.INDICATORS == list(INDICATORS)
    assert all(f"focus:annual:{name if name != 'PIB' else 'PIB Total'}" in CONTRACT_BY_ID
               for name in focus.INDICATORS)
    assert focus.FOCUS_12M_KEY == FOCUS_12M_KEY
