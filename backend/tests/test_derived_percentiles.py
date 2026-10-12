from datetime import datetime, timedelta, timezone

from derived.percentiles import (
    PercentileBand,
    PercentileObservation,
    empirical_percentile,
)

AS_OF = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def _observations(values: range) -> tuple[PercentileObservation, ...]:
    return tuple(
        PercentileObservation(value=float(value), as_of=AS_OF - timedelta(days=index))
        for index, value in enumerate(values)
    )


def test_midrank_percentile_handles_ties() -> None:
    observations = tuple(
        PercentileObservation(value=value, as_of=AS_OF - timedelta(days=index))
        for index, value in enumerate((*(1.0 for _ in range(12)), *(2.0 for _ in range(12))))
    )

    result = empirical_percentile(observations, current_value=1.0, as_of=AS_OF)

    assert result.percentile == 25.0
    assert result.band is PercentileBand.LOW
    assert result.reason is None


def test_twenty_three_finite_observations_are_insufficient() -> None:
    result = empirical_percentile(_observations(range(23)), current_value=10.0, as_of=AS_OF)

    assert result.percentile is None
    assert result.band is None
    assert result.reason == "insufficient_history"


def test_non_finite_values_do_not_count_toward_minimum() -> None:
    observations = _observations(range(23)) + (PercentileObservation(value=float("nan"), as_of=AS_OF),)

    result = empirical_percentile(observations, current_value=10.0, as_of=AS_OF)

    assert result.reason == "insufficient_history"


def test_percentile_band_boundaries_are_exact() -> None:
    observations = _observations(range(100))

    assert empirical_percentile(observations, current_value=19.0, as_of=AS_OF).band is PercentileBand.VERY_LOW
    assert empirical_percentile(observations, current_value=20.0, as_of=AS_OF).band is PercentileBand.LOW
    assert empirical_percentile(observations, current_value=40.0, as_of=AS_OF).band is PercentileBand.NEUTRAL
    assert empirical_percentile(observations, current_value=60.0, as_of=AS_OF).band is PercentileBand.HIGH
    assert empirical_percentile(observations, current_value=80.0, as_of=AS_OF).band is PercentileBand.VERY_HIGH


def test_observations_older_than_trailing_ten_year_window_are_excluded() -> None:
    recent = _observations(range(23))
    old = PercentileObservation(value=0.0, as_of=datetime(2016, 9, 26, 12, tzinfo=timezone.utc))

    result = empirical_percentile((*recent, old), current_value=10.0, as_of=AS_OF)

    assert result.reason == "insufficient_history"
