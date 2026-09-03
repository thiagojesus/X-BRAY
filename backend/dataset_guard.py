from typing import TypeVar


T = TypeVar("T")


class DatasetUnavailableError(RuntimeError):
    def __init__(self, dataset: str):
        self.dataset = dataset
        super().__init__(f"Dataset unavailable: {dataset}")


def require_records(dataset: str, records: list[T]) -> list[T]:
    if not records:
        raise DatasetUnavailableError(dataset)
    return records


def require_series_map(dataset: str, series: dict[str, list[T]]) -> dict[str, list[T]]:
    if not series or any(not records for records in series.values()):
        raise DatasetUnavailableError(dataset)
    return series
