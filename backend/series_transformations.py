from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, TypedDict, assert_never


TransformationMode = Literal["level", "mom", "yoy"]
SeriesValue = str | int | float | None


class TransformedRecord(TypedDict):
    data: str
    valor: SeriesValue


def _date(record: Mapping[str, object]) -> datetime:
    return datetime.strptime(str(record["data"]), "%d/%m/%Y")


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _period_values(records: Sequence[Mapping[str, object]]) -> dict[tuple[int, int], Decimal | None]:
    return {
        (parsed_date.year, parsed_date.month): _decimal(record.get("valor"))
        for record in records
        for parsed_date in (_date(record),)
    }


def _percentage_change(
    records: Sequence[Mapping[str, object]],
    *,
    lag_months: int,
) -> list[TransformedRecord]:
    values = _period_values(records)
    transformed: list[TransformedRecord] = []
    for record in records:
        current_date = _date(record)
        current_period = current_date.year * 12 + current_date.month - 1
        lag_period = current_period - lag_months
        previous = values.get((lag_period // 12, lag_period % 12 + 1))
        current = _decimal(record.get("valor"))
        value = None
        if current is not None and previous not in (None, Decimal(0)):
            value = float(((current / previous) - Decimal(1)) * Decimal(100))
        transformed.append({"data": str(record["data"]), "valor": value})
    return transformed


def level(records: Sequence[Mapping[str, object]]) -> list[TransformedRecord]:
    return [
        {
            "data": str(record["data"]),
            "valor": record.get("valor") if isinstance(record.get("valor"), (str, int, float)) else None,
        }
        for record in records
    ]


def month_over_month(records: Sequence[Mapping[str, object]]) -> list[TransformedRecord]:
    return _percentage_change(records, lag_months=1)


def year_over_year(records: Sequence[Mapping[str, object]]) -> list[TransformedRecord]:
    return _percentage_change(records, lag_months=12)


def transform_series(
    records: Sequence[Mapping[str, object]],
    mode: TransformationMode,
) -> list[TransformedRecord]:
    match mode:
        case "level":
            return level(records)
        case "mom":
            return month_over_month(records)
        case "yoy":
            return year_over_year(records)
        case unreachable:
            assert_never(unreachable)
