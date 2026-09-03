from datetime import date
from unittest.mock import MagicMock, patch

import datafetchers.anbima as anbima


def test_fetch_anbima_ima_uses_fresh_cache() -> None:
    # Given
    cached = {"ima_b": [{"Taxa": 6.2}]}

    # When
    with (
        patch("datafetchers.anbima._needs_refresh", return_value=False),
        patch("datafetchers.anbima.query_anbima", return_value=cached),
        patch("datafetchers.anbima.httpx.get") as provider_get,
    ):
        result = anbima.fetch_anbima_ima()

    # Then
    assert result == cached
    provider_get.assert_not_called()


def test_fetch_anbima_ima_parses_workbook_rows() -> None:
    # Given
    response = MagicMock()
    response.content = b"workbook"
    empty_sheet = MagicMock()
    empty_sheet.to_python.return_value = []
    data_sheet = MagicMock()
    data_sheet.to_python.return_value = [
        ["Data", "", "Taxa", "Opcional"],
        [date(2026, 9, 2), "", 6.2],
    ]
    workbook = MagicMock()
    workbook.sheet_names = [" Vazio ", " IMA-B 5 "]
    workbook.get_sheet_by_name.side_effect = lambda name: {
        " Vazio ": empty_sheet,
        " IMA-B 5 ": data_sheet,
    }[name]
    stored = {"ima_b_5": [{"Data": "2026-09-02", "col_1": None, "Taxa": 6.2, "Opcional": None}]}

    # When
    with (
        patch("datafetchers.anbima.httpx.get", return_value=response),
        patch("datafetchers.anbima.CalamineWorkbook.from_filelike", return_value=workbook),
        patch("datafetchers.anbima.upsert_anbima") as upsert,
        patch("datafetchers.anbima.set_meta") as set_meta,
        patch("datafetchers.anbima.query_anbima", return_value=stored),
    ):
        result = anbima.fetch_anbima_ima(use_cache=False)

    # Then
    response.raise_for_status.assert_called_once()
    upsert.assert_called_once_with("ima_b_5", stored["ima_b_5"])
    set_meta.assert_called_once()
    assert result == stored


def test_fetch_anbima_ima_returns_provider_error() -> None:
    # Given / When
    with patch("datafetchers.anbima.httpx.get", side_effect=RuntimeError("offline")):
        result = anbima.fetch_anbima_ima(use_cache=False)

    # Then
    assert result == {"error": "offline"}


def test_force_refresh_anbima_clears_timestamp() -> None:
    # Given
    refreshed = {"ima_b": [{"Taxa": 6.2}]}

    # When
    with (
        patch("datafetchers.anbima.set_meta") as set_meta,
        patch("datafetchers.anbima.fetch_anbima_ima", return_value=refreshed) as fetch,
    ):
        result = anbima.force_refresh_anbima()

    # Then
    set_meta.assert_called_once_with("anbima_last_refresh", "")
    fetch.assert_called_once_with(use_cache=False)
    assert result == refreshed


def test_read_anbima_ima_uses_cache_only_reader() -> None:
    # Given
    cached = {"ima_b": [{"Taxa": 6.2}]}

    # When
    with patch("datafetchers.anbima.query_anbima_all", return_value=cached) as query_all:
        result = anbima.read_anbima_ima()

    # Then
    assert result == cached
    query_all.assert_called_once()
