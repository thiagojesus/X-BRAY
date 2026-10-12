import json
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

import pytest

from db import store
from derived.di_selection import select_di_contract_for_horizon
from derived.real_rate import select_di_contract
from derived.real_rate_inputs import DIContract
from derived.sgs_inputs import load_sgs_observations
from indicator_catalog import Frequency, IndicatorKey, ProviderCode, Unit
from tests.real_rate_support import isolated_database


def _seed_sgs_vintage(
    series_key: str,
    reference_period: str,
    value: float,
    available_at: datetime,
    first_seen_at: datetime,
    source_published_at: datetime | None = None,
) -> None:
    raw_value = {"valor": value}
    raw_json = json.dumps(raw_value, separators=(",", ":"), allow_nan=False)
    payload_hash = sha256(raw_json.encode()).hexdigest()
    dimensions_json = "{}"
    dimensions_hash = sha256(dimensions_json.encode()).hexdigest()
    store.execute_change(
        "INSERT INTO observation_vintages "
        "(provider, series_key, reference_period, dimensions_json, dimensions_hash, "
        "raw_value_json, source_published_at, first_seen_at, available_at, payload_hash, revision_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "bcb_sgs",
            series_key,
            reference_period,
            dimensions_json,
            dimensions_hash,
            raw_json,
            source_published_at.isoformat() if source_published_at else None,
            first_seen_at.isoformat(),
            available_at.isoformat(),
            payload_hash,
            payload_hash[:16],
        ),
    )


def test_sgs_adapter_applies_catalog_sign_once_and_preserves_provenance() -> None:
    # Given
    cutoff = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    published = cutoff
    first_seen = cutoff - timedelta(hours=1)
    _seed_sgs_vintage(
        "saldo_primario_12m_pib", "2026-08-31", -5793.123456, published, first_seen, published
    )
    _seed_sgs_vintage("conta_corrente_12m_pib", "2026-08-31", -23079.876543, published, first_seen)

    # When
    surplus = load_sgs_observations(
        IndicatorKey("saldo_primario_12m_pib"), as_of=cutoff
    )[0]
    current_account = load_sgs_observations(
        IndicatorKey("conta_corrente_12m_pib"), as_of=cutoff
    )[0]

    # Then
    assert surplus.value == 5793.123456
    assert surplus.provider_code == ProviderCode(5793)
    assert surplus.unit is Unit.PCT_GDP
    assert surplus.frequency is Frequency.MONTHLY
    assert surplus.reference_period == date(2026, 8, 31)
    assert surplus.available_at == published
    assert surplus.source_published_at == published
    assert surplus.first_seen_at == first_seen
    assert current_account.value == -23079.876543


def test_sgs_adapter_excludes_observations_after_cutoff() -> None:
    # Given
    cutoff = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    published = cutoff + timedelta(seconds=1)
    _seed_sgs_vintage(
        "conta_corrente_12m_pib",
        "2026-08-31",
        2.5,
        published,
        cutoff,
        published,
    )

    # When
    observations = load_sgs_observations(
        IndicatorKey("conta_corrente_12m_pib"), as_of=cutoff
    )

    # Then
    assert observations == ()


def _contract(symbol: str, maturity: date) -> DIContract:
    return DIContract(
        trade_date=date(2026, 9, 25),
        symbol=symbol,
        maturity=maturity,
        rate=11.0,
        available_at=datetime(2026, 9, 25, 12, tzinfo=timezone.utc),
        availability_basis="first_seen_at",
    )


@pytest.mark.parametrize("horizon_days", [365, 1825])
@pytest.mark.parametrize("distance_days", [-90, 90])
def test_di_horizon_selector_accepts_day_ninety_boundary(
    horizon_days: int,
    distance_days: int,
) -> None:
    # Given
    reference = date(2026, 9, 25)
    boundary = _contract(
        "DI1BOUND",
        reference + timedelta(days=horizon_days + distance_days),
    )

    # When
    selected = select_di_contract_for_horizon((boundary,), reference, horizon_days)

    # Then
    assert selected == boundary


@pytest.mark.parametrize("horizon_days", [365, 1825])
def test_di_horizon_selector_rejects_contracts_beyond_inclusive_tolerance(
    horizon_days: int,
) -> None:
    # Given
    reference = date(2026, 9, 25)
    outside = reference + timedelta(days=horizon_days + 91)
    contract = _contract("DI1OUT", outside)

    # When
    selected = select_di_contract_for_horizon((contract,), reference, horizon_days)

    # Then
    assert selected is None


@pytest.mark.parametrize("horizon_days", [365, 1825])
def test_di_horizon_selector_keeps_tolerance_inclusive_and_tie_breaks(
    horizon_days: int,
) -> None:
    # Given
    reference = date(2026, 9, 25)
    target = reference + timedelta(days=horizon_days)
    earlier = _contract("DI1Z", target - timedelta(days=30))
    symbol_tie = _contract("DI1A", target - timedelta(days=30))
    boundary = _contract("DI1BOUND", target + timedelta(days=90))

    # When
    selected = select_di_contract_for_horizon(
        (boundary, earlier, symbol_tie), reference, horizon_days
    )

    # Then
    assert selected == symbol_tie


def test_real_rate_selector_remains_365_day_compatible() -> None:
    # Given
    reference = date(2026, 9, 25)
    contract = _contract("DI1V27", reference + timedelta(days=365))

    # When
    selected = select_di_contract((contract,), reference_date=reference)

    # Then
    assert selected == contract


pytestmark = pytest.mark.usefixtures(isolated_database.__name__)
