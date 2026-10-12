from series_transformations import month_over_month, year_over_year


def test_mom_uses_exact_previous_month_without_internal_rounding() -> None:
    transformed = month_over_month(
        [
            {"data": "01/12/2024", "valor": "100"},
            {"data": "01/01/2025", "valor": "110"},
        ]
    )

    assert transformed == [
        {"data": "01/12/2024", "valor": None},
        {"data": "01/01/2025", "valor": 10.0},
    ]


def test_yoy_uses_same_month_of_previous_year() -> None:
    transformed = year_over_year(
        [
            {"data": "01/01/2025", "valor": "100"},
            {"data": "01/01/2026", "valor": "121"},
        ]
    )

    assert transformed[-1] == {"data": "01/01/2026", "valor": 21.0}


def test_negative_values_follow_the_exact_ratio_formula() -> None:
    transformed = month_over_month(
        [
            {"data": "01/01/2025", "valor": "-100"},
            {"data": "01/02/2025", "valor": "-50"},
        ]
    )

    assert transformed[-1]["valor"] == -50.0


def test_zero_null_and_missing_denominators_are_unavailable() -> None:
    transformed = month_over_month(
        [
            {"data": "01/01/2025", "valor": "0"},
            {"data": "01/02/2025", "valor": "10"},
            {"data": "01/03/2025", "valor": None},
            {"data": "01/05/2025", "valor": "12"},
        ]
    )

    assert [record["valor"] for record in transformed] == [None, None, None, None]


def test_yoy_matches_calendar_month_across_leap_year() -> None:
    transformed = year_over_year(
        [
            {"data": "28/02/2023", "valor": "100"},
            {"data": "29/02/2024", "valor": "121"},
        ]
    )

    assert transformed[-1]["valor"] == 21.0


def test_non_numeric_values_are_unavailable() -> None:
    transformed = month_over_month(
        [
            {"data": "01/01/2025", "valor": "not-a-number"},
            {"data": "01/02/2025", "valor": "110"},
        ]
    )

    assert [record["valor"] for record in transformed] == [None, None]
