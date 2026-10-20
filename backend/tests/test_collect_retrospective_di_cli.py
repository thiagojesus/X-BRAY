import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

from scripts.collect_retrospective_di import (
    _input_paths,
    _month_start,
    _target_rows,
    main,
)


def test_month_start_and_input_path_expansion(tmp_path: Path) -> None:
    assert _month_start(date(2026, 1, 10), -1) == date(2025, 12, 1)
    xml = tmp_path / "one.xml"
    ignored = tmp_path / "ignored.txt"
    xml.write_text("<x/>", encoding="utf-8")
    ignored.write_text("x", encoding="utf-8")
    assert _input_paths([tmp_path]) == [xml]


def test_target_inventory_reads_current_rows_and_vintages_read_only(tmp_path: Path) -> None:
    database = tmp_path / "existing.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE b3_di (trade_date TEXT,symbol TEXT,maturity TEXT,rate REAL)")
        connection.execute("INSERT INTO b3_di VALUES ('2025-01-30','DI1F25','2025-02-03',12.0)")
        connection.execute("""CREATE TABLE observation_vintages (
            provider TEXT, series_key TEXT, reference_period TEXT,
            dimensions_json TEXT, raw_value_json TEXT)""")
        connection.execute(
            "INSERT INTO observation_vintages VALUES ('b3','di1','2025-02-27',?,?)",
            ('{"symbol":"DI1G25","maturity":"2025-03-03"}', '{"rate":13.0}'),
        )
    rows = _target_rows(database)
    assert {row["trade_date"] for row in rows} == {"2025-01-30", "2025-02-27"}
    assert _target_rows(tmp_path / "missing.db") == []


def test_target_inventory_rejects_corrupt_sqlite_before_collecting(tmp_path: Path) -> None:
    database = tmp_path / "corrupt.db"
    database.write_bytes(b"not sqlite")
    with pytest.raises(ValueError, match="could not inspect"):
        _target_rows(database)


def test_cli_default_is_inventory_only_and_does_not_call_collector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "isolated.db"
    xml = tmp_path / "source.xml"
    xml.write_text("<x/>", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "collect", "--sqlite", str(database), "--months", "1",
        "--cache-dir", str(tmp_path / "cache"), "--input", str(xml),
    ])
    monkeypatch.setattr("scripts.collect_retrospective_di.collect_candidates", lambda *_args, **_kwargs: pytest.fail("network path invoked in dry-run"))
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["minimum_interval_seconds"] == 30.0
    assert not database.exists()


def test_cli_rejects_less_than_30_second_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", [
        "collect", "--sqlite", str(tmp_path / "isolated.db"),
        "--min-request-interval", "1",
    ])
    with pytest.raises(SystemExit):
        main()


def test_cli_does_not_silently_treat_early_month_file_as_month_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    xml = tmp_path / "early.xml"
    xml.write_text(
        '<PriceReport xmlns="urn:bvmf.217.01.xsd"><PricRpt><TradDt><Dt>2026-09-10</Dt></TradDt>'
        '<SctyId><TckrSymb>DI1X27</TckrSymb></SctyId><FinInstrmAttrbts>'
        '<AdjstdQtTax>12.980</AdjstdQtTax></FinInstrmAttrbts></PricRpt></PriceReport>',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", [
        "collect", "--sqlite", str(tmp_path / "isolated.db"), "--months", "1",
        "--end", "2026-10-20", "--cache-dir", str(tmp_path / "cache"), "--input", str(xml),
    ])
    monkeypatch.setattr("scripts.collect_retrospective_di.collect_candidates", lambda *_args, **_kwargs: pytest.fail("network path invoked in dry-run"))
    assert main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["reused_month_candidates"] == []
    assert report["local_dates_outside_candidate_window"][0]["month"] == "2026-09"
    assert report["requests_planned_upper_bound"] > 0
