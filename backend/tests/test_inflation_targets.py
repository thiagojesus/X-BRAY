from datetime import date
from itertools import pairwise

import pytest

from derived.inflation_targets import (
    INFLATION_TARGET_RULES,
    INFLATION_TARGET_TABLE_VERSION,
    OFFICIAL_HISTORICAL_TABLE_URL,
    target_for_reference_date,
)


@pytest.mark.parametrize(
    ("reference_date", "rule_id", "center", "band"),
    [
        (date(2015, 1, 1), "bcb-cmn-target-2015", 4.50, 2.00),
        (date(2016, 12, 31), "bcb-cmn-target-2016", 4.50, 2.00),
        (date(2017, 1, 1), "bcb-cmn-target-2017", 4.50, 1.50),
        (date(2018, 12, 31), "bcb-cmn-target-2018", 4.50, 1.50),
        (date(2019, 6, 15), "bcb-cmn-target-2019", 4.25, 1.50),
        (date(2020, 6, 15), "bcb-cmn-target-2020", 4.00, 1.50),
        (date(2021, 6, 15), "bcb-cmn-target-2021", 3.75, 1.50),
        (date(2022, 6, 15), "bcb-cmn-target-2022", 3.50, 1.50),
        (date(2023, 6, 15), "bcb-cmn-target-2023", 3.25, 1.50),
        (date(2024, 12, 31), "bcb-cmn-target-2024", 3.00, 1.50),
        (date(2025, 1, 1), "bcb-continuous-target-2025", 3.00, 1.50),
        (date(2035, 8, 20), "bcb-continuous-target-2025", 3.00, 1.50),
    ],
)
def test_target_rule_matches_reference_date_and_official_values(
    reference_date: date,
    rule_id: str,
    center: float,
    band: float,
) -> None:
    # Given

    # When
    rule = target_for_reference_date(reference_date)

    # Then
    assert rule is not None
    assert rule.rule_id == rule_id
    assert rule.center_pct == center
    assert rule.tolerance_band_pp == band
    assert rule.table_version == INFLATION_TARGET_TABLE_VERSION == "1.0.0"
    assert rule.source_url == OFFICIAL_HISTORICAL_TABLE_URL
    assert rule.regime
    assert rule.legal_reference is None
    assert rule.direct_source_url is None
    assert not hasattr(rule, "available_at")


@pytest.mark.parametrize(
    ("reference_date", "rule_id"),
    [
        (date(2016, 12, 31), "bcb-cmn-target-2016"),
        (date(2017, 1, 1), "bcb-cmn-target-2017"),
        (date(2024, 12, 31), "bcb-cmn-target-2024"),
        (date(2025, 1, 1), "bcb-continuous-target-2025"),
    ],
)
def test_target_rule_boundaries_are_half_open(
    reference_date: date,
    rule_id: str,
) -> None:
    # Given

    # When
    rule = target_for_reference_date(reference_date)

    # Then
    assert rule is not None
    assert rule.rule_id == rule_id


@pytest.mark.parametrize("reference_date", [date.min, date(2014, 12, 31)])
def test_target_rule_is_absent_before_supported_history(reference_date: date) -> None:
    # Given

    # When
    rule = target_for_reference_date(reference_date)

    # Then
    assert rule is None


def test_rule_table_is_an_immutable_sequential_effective_timeline() -> None:
    # Given
    rules = INFLATION_TARGET_RULES

    # When
    # Then
    assert isinstance(rules, tuple)
    assert len(rules) == 11
    assert all(
        earlier.effective_to == later.effective_from
        for earlier, later in pairwise(rules)
    )
    assert rules[-1].effective_to is None
