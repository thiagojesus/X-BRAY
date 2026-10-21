import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from db import store
from derived.repository import query_latest_snapshots, query_snapshot_history, refresh_derived_snapshots
from main import app
from tests.real_rate_support import REAL_RATE_KEY, isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)

AS_OF = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
SOURCE_AVAILABLE = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
FOCUS_KEY = "IPCA_12M_S_BASE0"


def _month_end(month_index: int) -> date:
    year, zero_month = divmod(month_index, 12)
    next_month = date(year + (zero_month == 11), zero_month % 12 + 1, 1)
    return next_month - timedelta(days=1)


def _seed_focus(reference: date, available_at: datetime) -> None:
    record = {
        "Indicador": "IPCA",
        "Data": reference.isoformat(),
        "Mediana": 4.0 + (reference.month / 100),
        "Suavizada": "S",
        "baseCalculo": 0,
    }
    with patch("db.store._utc_now", return_value=available_at.isoformat()):
        store.upsert_focus(FOCUS_KEY, [record])


def _seed_current_inputs() -> None:
    reference = date(2026, 10, 16)
    _seed_focus(reference, SOURCE_AVAILABLE)
    with patch("db.store._utc_now", return_value=SOURCE_AVAILABLE.isoformat()):
        store.upsert_b3_di(
            [
                {
                    "trade_date": reference.isoformat(),
                    "maturity": (reference + timedelta(days=365)).isoformat(),
                    "symbol": "DI1CURRENT",
                    "rate": 13.0,
                }
            ]
        )


def _seed_instrument(
    *, symbol: str, maturity: date, fin_id: str, source_hash: str, available_at: datetime
) -> None:
    store.execute_change(
        "INSERT INTO b3_di_instrument_snapshots "
        "(snapshot_date,report_date,symbol,security_id,fin_instrm_id,maturity,trading_start,trading_end,"
        "source_hash,source_name,message_definition,message_id,captured_at,available_at,source_basis) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "2026-10-20",
            "2026-10-20",
            symbol,
            f"ISIN-{fin_id}",
            fin_id,
            maturity.isoformat(),
            None,
            None,
            source_hash,
            "official-registry.xml",
            "bvmf.100.02",
            f"message-{fin_id}",
            available_at.isoformat(),
            available_at.isoformat(),
            "b3_instrument_snapshot_v1",
        ),
    )


def _seed_retrospective_month(
    reference: date,
    index: int,
    *,
    available_at: datetime = SOURCE_AVAILABLE,
    registry_available_at: datetime | None = None,
    future_focus: bool = False,
    wrong_registry_identity: bool = False,
) -> None:
    symbol = f"DI1H{index:02d}"
    fin_id = f"FIN-{index}"
    source_hash = hashlib.sha256(f"price-{index}".encode()).hexdigest()
    maturity = reference + timedelta(days=365)
    focus_date = reference + timedelta(days=1) if future_focus else reference
    _seed_focus(focus_date, available_at)
    store.execute_change(
        "INSERT INTO retrospective_di_observations "
        "(trade_date,symbol,maturity,rate,source_hash,fetched_at,revision,source_basis,source_name,"
        "source_member,maturity_basis,security_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            reference.isoformat(),
            symbol,
            maturity.isoformat(),
            10.0 + index / 100,
            source_hash,
            available_at.isoformat(),
            f"revision-{index}",
            "retrospective_reference",
            "price-report.xml",
            f"price-report.xml!{symbol}",
            "b3_instrument_snapshot_v1",
            fin_id,
        ),
    )
    _seed_instrument(
        symbol=symbol,
        maturity=maturity,
        fin_id=f"OTHER-{fin_id}" if wrong_registry_identity else fin_id,
        source_hash=source_hash,
        available_at=registry_available_at or available_at,
    )


def _seed_history(
    history_count: int,
    *,
    future_last_instrument: bool = False,
    future_last_focus: bool = False,
    wrong_last_identity: bool = False,
) -> None:
    first_month = 2024 * 12
    for index in range(history_count):
        reference = _month_end(first_month + index)
        registry_available = (
            AS_OF + timedelta(days=1)
            if future_last_instrument and index == history_count - 1
            else SOURCE_AVAILABLE
        )
        _seed_retrospective_month(
            reference,
            index,
            available_at=SOURCE_AVAILABLE,
            registry_available_at=registry_available,
            future_focus=future_last_focus and index == history_count - 1,
            wrong_registry_identity=wrong_last_identity and index == history_count - 1,
        )


@pytest.mark.parametrize(
    ("history_count", "expected_count", "expected_reason"),
    [(22, 23, "insufficient_history"), (23, 24, None)],
)
def test_refresh_persists_monthly_reconstructed_history_and_api_classification(
    history_count: int, expected_count: int, expected_reason: str | None
) -> None:
    _seed_history(history_count)
    _seed_current_inputs()

    assert refresh_derived_snapshots(AS_OF, "retrospective-monthly-cycle") == 1
    result = query_latest_snapshots()[REAL_RATE_KEY].result

    assert result.value is not None
    assert result.classification_history_count == expected_count
    assert result.classification_history_basis == (
        "retrospective_reference_monthly_plus_production_forward_captured_monthly"
    )
    assert result.classification_history_available_at == SOURCE_AVAILABLE
    assert result.classification_reason == expected_reason
    snapshots = query_snapshot_history(REAL_RATE_KEY)
    assert len(snapshots) == 1
    with TestClient(app) as client:
        response = client.get("/api/derivados/juro-real-ex-ante-1y")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["value"] == result.value
    assert payload["classification_history_count"] == expected_count
    assert payload["classification_history_basis"] == result.classification_history_basis
    assert payload["classification_history_available_at"] == SOURCE_AVAILABLE.isoformat()
    assert payload["historical_percentile"] == result.historical_percentile


@pytest.mark.parametrize(
    "invalid_last_source",
    ["future_instrument", "future_focus", "wrong_identity"],
)
def test_history_gate_excludes_unavailable_or_unverified_reconstructed_month(
    invalid_last_source: str,
) -> None:
    _seed_history(
        23,
        future_last_instrument=invalid_last_source == "future_instrument",
        future_last_focus=invalid_last_source == "future_focus",
        wrong_last_identity=invalid_last_source == "wrong_identity",
    )
    _seed_current_inputs()

    assert refresh_derived_snapshots(AS_OF, f"invalid-retro-{invalid_last_source}") == 1
    result = query_latest_snapshots()[REAL_RATE_KEY].result

    assert result.value is not None
    assert result.classification_history_count == 23
    assert result.historical_percentile is None
    assert result.classification is None
    assert result.classification_reason == "insufficient_history"


def test_retrospective_history_uses_official_fisher_formula_and_monthly_reference() -> None:
    _seed_history(1)

    from derived.retrospective_real_rate import load_retrospective_real_rate_history

    history = load_retrospective_real_rate_history(as_of=AS_OF)

    assert len(history) == 1
    point = history[0]
    inputs = {item.key: item for item in point.inputs}
    expected = (
        (1 + inputs["nominal_rate"].value / 100)
        / (1 + inputs["expected_inflation"].value / 100)
        - 1
    ) * 100
    assert point.value == pytest.approx(expected)
    assert point.formula == "((1 + DI/100) / (1 + IPCA/100) - 1) * 100"
    assert point.reference_date == _month_end(2024 * 12).isoformat()
    assert all(item.available_at <= AS_OF for item in point.inputs)
    focus_metadata = dict(inputs["expected_inflation"].metadata)
    assert focus_metadata["series_key"] == FOCUS_KEY
    assert focus_metadata["filter"] == (
        "Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0"
    )
    assert focus_metadata["source_formula_version"] == "macro-retrospective-di-2.0.0"


@pytest.mark.parametrize(
    "row",
    [
        (),
        ("other", "2024-12", 1.0, "pct_annual", "version", "basis", "now", "{}", "hash", None),
        (REAL_RATE_KEY, "2024-12", 1.0, "wrong-unit", "version", "basis", "now", "{}", "hash", None),
        (REAL_RATE_KEY, "2024-12", 1.0, "pct_annual", "wrong-version", "basis", "now", "{}", "hash", None),
        (REAL_RATE_KEY, "2024-12", 1.0, "pct_annual", "version", "wrong-basis", "now", "{}", "hash", None),
        (REAL_RATE_KEY, "2024-12", "bad-value", "pct_annual", "version", "basis", "now", "{}", "hash", None),
        (REAL_RATE_KEY, "2024-12", 1.0, "pct_annual", "version", "basis", "now", "not-json", "hash", None),
    ],
)
def test_retrospective_row_rejects_malformed_or_incompatible_source(row: tuple[object, ...]) -> None:
    from derived.retrospective_real_rate import _as_result

    assert _as_result(row, cutoff=AS_OF, registry=()) is None


def test_retrospective_row_rejects_unaligned_or_invalid_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from derived.retrospective_real_rate import _as_result
    from macro_score.retrospective import DI_FORMULA_VERSION

    monkeypatch.setattr(
        "macro_score.retrospective.has_verified_di_maturities",
        lambda _payload, *, registry: True,
    )
    payload = {
        "reference_date": "2024-12-31",
        "di_1y": {"rate": 10.0, "trade_date": "2024-12-31", "fetched_at": SOURCE_AVAILABLE.isoformat()},
        "focus_12m_base0": {"value": 4.0, "reference_date": "2024-12-31", "available_at": SOURCE_AVAILABLE.isoformat()},
    }
    base: tuple[object, ...] = (
        REAL_RATE_KEY,
        "2024-12",
        5.76923076923077,
        "pct_annual",
        DI_FORMULA_VERSION,
        "retrospective_reference",
        SOURCE_AVAILABLE.isoformat(),
        json.dumps(payload),
        "fingerprint",
        None,
    )

    assert _as_result(base, cutoff=AS_OF, registry=()) is not None
    bad_month = (*base[:1], "2025-01", *base[2:])
    assert _as_result(bad_month, cutoff=AS_OF, registry=()) is None
    bad_date_payload = {**payload, "reference_date": "not-a-date"}
    bad_date = (*base[:7], json.dumps(bad_date_payload), *base[8:])
    assert _as_result(bad_date, cutoff=AS_OF, registry=()) is None
    future_cutoff = (*base[:6], (AS_OF + timedelta(days=1)).isoformat(), *base[7:])
    assert _as_result(future_cutoff, cutoff=AS_OF, registry=()) is None
    missing_di = {**payload, "di_1y": None}
    missing = (*base[:7], json.dumps(missing_di), *base[8:])
    assert _as_result(missing, cutoff=AS_OF, registry=()) is None
    zero_focus = {
        **payload,
        "focus_12m_base0": {
            **payload["focus_12m_base0"],
            "value": -100,
        },
    }
    zero = (*base[:7], json.dumps(zero_focus), *base[8:])
    assert _as_result(zero, cutoff=AS_OF, registry=()) is None


def test_retrospective_row_rejects_bad_timestamps_values_and_formula(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from derived.retrospective_real_rate import _as_result, _record, _timestamp
    from macro_score.retrospective import DI_FORMULA_VERSION

    monkeypatch.setattr(
        "macro_score.retrospective.has_verified_di_maturities",
        lambda _payload, *, registry: True,
    )
    payload = {
        "reference_date": "2024-12-31",
        "di_1y": {"rate": 10.0, "trade_date": "2024-12-31", "fetched_at": SOURCE_AVAILABLE.isoformat()},
        "focus_12m_base0": {"value": 4.0, "reference_date": "2024-12-31", "available_at": SOURCE_AVAILABLE.isoformat()},
    }
    base: tuple[object, ...] = (
        REAL_RATE_KEY,
        "2024-12",
        5.76923076923077,
        "pct_annual",
        DI_FORMULA_VERSION,
        "retrospective_reference",
        SOURCE_AVAILABLE.isoformat(),
        json.dumps(payload),
        "fingerprint",
        None,
    )

    assert _timestamp(None) is None
    assert _timestamp("not-a-timestamp") is None
    assert _record(None) is None
    assert _as_result((*base[:7], "{", *base[8:]), cutoff=AS_OF, registry=()) is None
    assert _as_result((*base[:2], float("nan"), *base[3:]), cutoff=AS_OF, registry=()) is None
    assert _as_result((*base[:2], "not-numeric", *base[3:]), cutoff=AS_OF, registry=()) is None
    assert _as_result((*base[:6], "bad-time", *base[7:]), cutoff=AS_OF, registry=()) is None
    monkeypatch.setattr(
        "macro_score.retrospective.has_verified_di_maturities",
        lambda _payload, *, registry: False,
    )
    assert _as_result(base, cutoff=AS_OF, registry=()) is None
    monkeypatch.setattr(
        "macro_score.retrospective.has_verified_di_maturities",
        lambda _payload, *, registry: True,
    )

    bad_rate_inputs = {
        **payload,
        "di_1y": {**payload["di_1y"], "rate": "not-numeric"},
    }
    bad_rate = (*base[:7], json.dumps(bad_rate_inputs), *base[8:])
    assert _as_result(bad_rate, cutoff=AS_OF, registry=()) is None
    future_focus = {
        **payload,
        "focus_12m_base0": {
            **payload["focus_12m_base0"],
            "available_at": (AS_OF + timedelta(days=1)).isoformat(),
        },
    }
    future = (*base[:7], json.dumps(future_focus), *base[8:])
    assert _as_result(future, cutoff=AS_OF, registry=()) is None
    wrong_result = (*base[:2], 1.0, *base[3:])
    assert _as_result(wrong_result, cutoff=AS_OF, registry=()) is None
