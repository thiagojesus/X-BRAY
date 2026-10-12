from dataclasses import dataclass
from datetime import date
from typing import Final

INFLATION_TARGET_TABLE_VERSION: Final = "1.0.0"
OFFICIAL_HISTORICAL_TABLE_URL: Final = "https://www.bcb.gov.br/controleinflacao/historicometas"


@dataclass(frozen=True, slots=True)
class InflationTargetRule:
    rule_id: str
    table_version: str
    regime: str
    effective_from: date
    effective_to: date | None
    center_pct: float
    tolerance_band_pp: float
    source_url: str
    legal_reference: str | None = None
    direct_source_url: str | None = None


INFLATION_TARGET_RULES: Final[tuple[InflationTargetRule, ...]] = (
    InflationTargetRule(
        "bcb-cmn-target-2015",
        INFLATION_TARGET_TABLE_VERSION,
        "annual_target",
        date(2015, 1, 1),
        date(2016, 1, 1),
        4.50,
        2.00,
        OFFICIAL_HISTORICAL_TABLE_URL,
    ),
    InflationTargetRule(
        "bcb-cmn-target-2016",
        INFLATION_TARGET_TABLE_VERSION,
        "annual_target",
        date(2016, 1, 1),
        date(2017, 1, 1),
        4.50,
        2.00,
        OFFICIAL_HISTORICAL_TABLE_URL,
    ),
    InflationTargetRule(
        "bcb-cmn-target-2017",
        INFLATION_TARGET_TABLE_VERSION,
        "annual_target",
        date(2017, 1, 1),
        date(2018, 1, 1),
        4.50,
        1.50,
        OFFICIAL_HISTORICAL_TABLE_URL,
    ),
    InflationTargetRule(
        "bcb-cmn-target-2018",
        INFLATION_TARGET_TABLE_VERSION,
        "annual_target",
        date(2018, 1, 1),
        date(2019, 1, 1),
        4.50,
        1.50,
        OFFICIAL_HISTORICAL_TABLE_URL,
    ),
    InflationTargetRule(
        "bcb-cmn-target-2019",
        INFLATION_TARGET_TABLE_VERSION,
        "annual_target",
        date(2019, 1, 1),
        date(2020, 1, 1),
        4.25,
        1.50,
        OFFICIAL_HISTORICAL_TABLE_URL,
    ),
    InflationTargetRule(
        "bcb-cmn-target-2020",
        INFLATION_TARGET_TABLE_VERSION,
        "annual_target",
        date(2020, 1, 1),
        date(2021, 1, 1),
        4.00,
        1.50,
        OFFICIAL_HISTORICAL_TABLE_URL,
    ),
    InflationTargetRule(
        "bcb-cmn-target-2021",
        INFLATION_TARGET_TABLE_VERSION,
        "annual_target",
        date(2021, 1, 1),
        date(2022, 1, 1),
        3.75,
        1.50,
        OFFICIAL_HISTORICAL_TABLE_URL,
    ),
    InflationTargetRule(
        "bcb-cmn-target-2022",
        INFLATION_TARGET_TABLE_VERSION,
        "annual_target",
        date(2022, 1, 1),
        date(2023, 1, 1),
        3.50,
        1.50,
        OFFICIAL_HISTORICAL_TABLE_URL,
    ),
    InflationTargetRule(
        "bcb-cmn-target-2023",
        INFLATION_TARGET_TABLE_VERSION,
        "annual_target",
        date(2023, 1, 1),
        date(2024, 1, 1),
        3.25,
        1.50,
        OFFICIAL_HISTORICAL_TABLE_URL,
    ),
    InflationTargetRule(
        "bcb-cmn-target-2024",
        INFLATION_TARGET_TABLE_VERSION,
        "annual_target",
        date(2024, 1, 1),
        date(2025, 1, 1),
        3.00,
        1.50,
        OFFICIAL_HISTORICAL_TABLE_URL,
    ),
    InflationTargetRule(
        "bcb-continuous-target-2025",
        INFLATION_TARGET_TABLE_VERSION,
        "continuous_target",
        date(2025, 1, 1),
        None,
        3.00,
        1.50,
        OFFICIAL_HISTORICAL_TABLE_URL,
    ),
)


def target_for_reference_date(reference_date: date) -> InflationTargetRule | None:
    for rule in INFLATION_TARGET_RULES:
        if rule.effective_from <= reference_date and (
            rule.effective_to is None or reference_date < rule.effective_to
        ):
            return rule
    return None
