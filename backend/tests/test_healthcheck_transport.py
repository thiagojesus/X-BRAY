import json
import sys
from http.client import RemoteDisconnected
from unittest.mock import patch

import pytest

from healthcheck import ENDPOINTS, main


def test_main_writes_health_report_when_refresh_connection_closes(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    output = tmp_path / "health-report.json"
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "test-token")
    monkeypatch.setenv("XBRAY_REFRESH_RUN_ID", "workflow-run-123")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "healthcheck.py",
            "--base-url",
            "https://api.example.com",
            "--refresh",
            "--output",
            str(output),
        ],
    )
    endpoint_result = {
        "path": "/api/status",
        "status": "ok",
        "http_status": 200,
        "latency_seconds": 0.1,
    }

    with (
        patch("healthcheck.urlopen", side_effect=RemoteDisconnected("closed")) as open_url,
        patch("healthcheck.time.sleep"),
        patch("healthcheck.check_endpoint", return_value=endpoint_result) as check_endpoint,
    ):
        exit_code = main()

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["status"] == "error"
    assert report["refresh"]["status"] == "error"
    assert json.loads(capsys.readouterr().out) == report
    assert check_endpoint.call_count == len(ENDPOINTS)
    methods = [call.args[0].method for call in open_url.call_args_list]
    assert methods == ["POST", "GET", "GET", "GET"]


def test_request_json_classifies_connection_reset_as_transport_failure() -> None:
    from healthcheck import HealthcheckProbeError, request_json

    with (
        patch("healthcheck.urlopen", side_effect=ConnectionResetError(104, "connection reset")),
        pytest.raises(HealthcheckProbeError, match="remote connection ended"),
    ):
        request_json("https://api.example.com/api/refresh-runs/run/advance", "POST", 30)


def test_main_writes_sanitized_report_for_unexpected_runtime_failure(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    output = tmp_path / "health-report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["healthcheck.py", "--base-url", "https://api.example.com", "--refresh", "--output", str(output)],
    )
    with patch("healthcheck.run", side_effect=RuntimeError("token=must-not-leak")):
        exit_code = main()

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["status"] == "error"
    assert report["refresh"]["status"] == "error"
    assert report["failures"] == ["healthcheck terminated unexpectedly (RuntimeError)"]
    captured = capsys.readouterr()
    assert "must-not-leak" not in captured.out + captured.err
    assert "healthcheck terminated unexpectedly (RuntimeError)" in captured.err
