from datetime import date

import pytest

from credit_exposures import build_credit_exposures

CHEQUE = "carteira_pf_cheque_especial"
PERSONAL = "carteira_pf_credito_pessoal_nao_consignado"
ROTATIVO = "carteira_pf_cartao_rotativo"
PARCELADO = "carteira_pf_cartao_parcelado"


def _records(*points: tuple[str, str]) -> list[dict[str, str]]:
    return [{"data": reference, "valor": value} for reference, value in points]


def _build(records: dict[str, object], as_of: date = date(2026, 10, 29)):
    return build_credit_exposures(records, as_of=as_of)


def test_monthly_growth_uses_exact_calendar_months_and_percentage_not_pp() -> None:
    result = _build({CHEQUE: _records(
        ("2025-01-31", "100"), ("2025-11-30", "100"), ("2026-01-31", "120"),
    )})[CHEQUE]

    assert result["latest"] == {"reference": "2026-01-31", "value": 120.0, "reason": None}
    assert result["mom"]["value_pct"] is None
    assert result["mom"]["reason"] == "missing_base"
    assert result["mom"]["base_reference"] == "2025-12-01"
    assert result["yoy"]["value_pct"] == pytest.approx(20.0)
    assert result["yoy"]["base_reference"] == "2025-01-01"
    assert result["yoy"]["base_observation_reference"] == "2025-01-31"
    assert result["history"][11]["reference"] == "2025-12-01"
    assert result["history"][11]["value"] is None
    assert result["history"][11]["mom_reason"] == "missing_current"


def test_calendar_leap_and_year_boundaries_with_shuffled_input() -> None:
    result = _build({ROTATIVO: _records(
        ("29/02/2024", "50"), ("2025-02-28", "60"), ("2025-01-31", "55"),
    )})[ROTATIVO]

    assert result["mom"]["value_pct"] == pytest.approx((60 / 55 - 1) * 100)
    assert result["mom"]["base_reference"] == "2025-01-01"
    assert result["mom"]["base_observation_reference"] == "2025-01-31"
    assert result["yoy"]["value_pct"] == pytest.approx(20)
    assert result["yoy"]["base_reference"] == "2024-02-01"
    assert result["yoy"]["base_observation_reference"] == "2024-02-29"


def test_month_duplicate_uses_latest_observation_in_month() -> None:
    result = _build({PERSONAL: _records(
        ("01/01/2026", "10"), ("31/01/2026", "20"), ("01/02/2026", "30"),
    )})[PERSONAL]
    assert result["history"][0]["value"] == 20
    assert result["mom"]["value_pct"] == pytest.approx(50)
    assert result["mom"]["base_reference"] == "2026-01-01"
    assert result["mom"]["base_observation_reference"] == "2026-01-31"


@pytest.mark.parametrize(
    ("points", "mom_reason", "yoy_reason"),
    [
        ([ ("2025-01-31", "0"), ("2026-01-31", "0") ], "missing_base", "zero_base"),
        ([ ("2025-01-31", "10"), ("2026-01-31", "0") ], "missing_base", None),
        ([ ("2025-02-28", "10"), ("2026-01-31", "20") ], "missing_base", "missing_base"),
    ],
)
def test_growth_handles_zero_and_exact_base_absence(
    points: list[tuple[str, str]], mom_reason: str, yoy_reason: str | None,
) -> None:
    result = _build({PARCELADO: _records(*points)})[PARCELADO]
    assert result["mom"]["reason"] == mom_reason
    assert result["yoy"]["reason"] == yoy_reason
    if points[-1][1] == "0" and points[0][1] == "10":
        assert result["yoy"]["value_pct"] == -100


@pytest.mark.parametrize("bad", [None, True, "NaN", "Infinity", "12junk", "-1", "1e999"])
def test_invalid_stock_values_never_become_zero_or_nonfinite(bad: object) -> None:
    result = _build({CHEQUE: [
        {"data": "2026-01-31", "valor": bad},
    ]})[CHEQUE]
    assert result["unavailable"] is True
    assert result["latest"]["value"] is None
    assert result["latest"]["reason"] == "invalid_current"


@pytest.mark.parametrize("bad_date", ["2026-02-30", "nonsense", "01/13/2026", "2035-01-31"])
def test_bad_or_future_dates_are_rejected(bad_date: str) -> None:
    result = _build({CHEQUE: _records((bad_date, "15"))})[CHEQUE]
    assert result["unavailable"] is True
    assert result["latest"]["value"] is None


def test_future_reference_is_compared_to_explicit_as_of_date() -> None:
    records = {CHEQUE: _records(("2026-01-31", "15"))}
    before = build_credit_exposures(records, as_of=date(2025, 12, 31))[CHEQUE]
    after = build_credit_exposures(records, as_of=date(2026, 1, 31))[CHEQUE]
    assert before["unavailable"] is True
    assert before["latest"]["reason"] == "invalid_current"
    assert after["unavailable"] is False
    assert after["latest"]["reference"] == "2026-01-31"


def test_nonfinite_growth_is_null_with_invalid_result_reason() -> None:
    result = _build({CHEQUE: _records(
        ("2025-01-31", "1e-308"), ("2026-01-31", "1e308"),
    )})[CHEQUE]
    assert result["yoy"]["value_pct"] is None
    assert result["yoy"]["reason"] == "invalid_result"


def test_invalid_base_has_distinct_reason_from_absent_base() -> None:
    result = _build({CHEQUE: [
        {"data": "2025-01-31", "valor": "10"},
        {"data": "2025-12-31", "valor": "-1"},
        {"data": "2026-01-31", "valor": "20"},
    ]})[CHEQUE]
    assert result["mom"]["value_pct"] is None
    assert result["mom"]["reason"] == "invalid_base"


def test_metadata_is_nominal_selected_free_pf_and_all_four_keys_always_exist() -> None:
    result = _build({})
    assert set(result) == {CHEQUE, PERSONAL, ROTATIVO, PARCELADO}
    assert result[PERSONAL]["code"] == 20574
    assert "não consignado total" in result[PERSONAL]["official_label"].lower()
    assert result[PARCELADO]["code"] == 20588
    assert result[PARCELADO]["unit"] == "brl_million"
    assert result[PARCELADO]["universe"] == "Pessoas físicas — recursos livres"
    assert "nominal" in result[PARCELADO]["measure"].lower()
    assert result[PARCELADO]["unavailable"] is True
