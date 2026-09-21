import pytest

from domain_contracts import Record
from domains import fiscal
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey


@pytest.mark.parametrize(
    ("indicator", "raw_value", "expected_value"),
    [
        ("saldo_primario_12m_pib", "-1.20", "1.20"),
        ("saldo_nominal_12m_pib", "2.50", "-2.50"),
        ("juros_nominais_12m_pib", "6.00", "6.00"),
    ],
)
def test_present_record_applies_fiscal_sign_without_mutating_raw(
    indicator: str,
    raw_value: str,
    expected_value: str,
) -> None:
    record: Record = {"data": "01/07/2026", "valor": raw_value}
    raw_record = record.copy()

    presented = fiscal.present_record(
        record,
        INDICATOR_CATALOG[IndicatorKey(indicator)],
    )

    assert presented["valor"] == expected_value
    assert record == raw_record
