import json
import sys
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.error import HTTPError, URLError
from unittest.mock import MagicMock, patch

import pytest

from healthcheck import (
    ENDPOINTS,
    EndpointSpec,
    check_endpoint,
    check_refresh,
    freshness_hours,
    main,
    request_json,
    run,
    validate_payload,
)


def _http_response(body: str, status: int = 200) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.read.return_value = body.encode("utf-8")
    response.__enter__.return_value = response
    return response


@pytest.mark.parametrize(("method", "request_body"), [("GET", None), ("POST", b"")])
def test_request_json_builds_expected_request(method: str, request_body: bytes | None) -> None:
    # Given
    response = _http_response('{"status": "ok"}')

    # When
    with patch("healthcheck.urlopen", return_value=response) as open_url:
        status, payload, elapsed = request_json("https://api.example.com/path", method, 3)

    # Then
    request = open_url.call_args.args[0]
    assert request.method == method
    assert request.data == request_body
    assert status == 200
    assert payload == {"status": "ok"}
    assert elapsed >= 0


def test_request_json_decodes_http_error_body() -> None:
    # Given
    error = HTTPError(
        "https://api.example.com/path",
        503,
        "unavailable",
        hdrs=None,
        fp=BytesIO(b'{"error": "unavailable"}'),
    )

    # When
    with patch("healthcheck.urlopen", side_effect=error):
        status, payload, _ = request_json("https://api.example.com/path", "GET", 3)

    # Then
    assert status == 503
    assert payload == {"error": "unavailable"}


def test_request_json_rejects_invalid_json() -> None:
    # Given
    response = _http_response("not-json")

    # When / Then
    with (
        patch("healthcheck.urlopen", return_value=response),
        pytest.raises(RuntimeError, match="invalid JSON"),
    ):
        request_json("https://api.example.com/path", "GET", 3)


def test_payload_validation_handles_strings_scalars_and_missing_paths() -> None:
    # Given
    payload = {"label": " valid ", "count": 0}

    # When
    validate_payload(payload, (("label",), ("count",)))

    # Then
    with pytest.raises(ValueError, match="missing JSON path"):
        validate_payload(payload, (("absent",),))


def test_payload_validation_rejects_blank_string() -> None:
    # Given / When / Then
    with pytest.raises(ValueError, match="empty JSON path: label"):
        validate_payload({"label": "   "}, (("label",),))


def test_freshness_hours_rejects_missing_timestamp() -> None:
    # Given / When / Then
    with pytest.raises(ValueError, match="last_updated is missing"):
        freshness_hours(None)


def test_freshness_hours_accepts_naive_timestamp() -> None:
    # Given
    updated = datetime.now() - timedelta(minutes=30)

    # When
    age = freshness_hours(updated.isoformat())

    # Then
    assert 0.4 <= age <= 0.6


def test_check_refresh_accepts_all_successful_tasks() -> None:
    # Given
    tasks = [{"name": f"task-{index}", "status": "ok"} for index in range(14)]

    # When
    with patch("healthcheck.request_json", return_value=(200, {"status": "ok", "tasks": tasks}, 1.2345)):
        result = check_refresh("https://api.example.com")

    # Then
    assert result["status"] == "ok"
    assert result["tasks"] == tasks
    assert result["latency_seconds"] == 1.234


def test_check_refresh_rejects_http_failure() -> None:
    # Given / When / Then
    with (
        patch("healthcheck.request_json", return_value=(503, {"error": "down"}, 0.1)),
        pytest.raises(RuntimeError, match="HTTP 503"),
    ):
        check_refresh("https://api.example.com")


def test_check_refresh_rejects_failed_payload() -> None:
    # Given / When / Then
    with (
        patch("healthcheck.request_json", return_value=(200, {"status": "error"}, 0.1)),
        pytest.raises(RuntimeError, match="refresh failed"),
    ):
        check_refresh("https://api.example.com")


def test_check_refresh_requires_all_tasks() -> None:
    # Given / When / Then
    with (
        patch("healthcheck.request_json", return_value=(200, {"status": "ok", "tasks": []}, 0.1)),
        pytest.raises(RuntimeError, match="all 14 tasks"),
    ):
        check_refresh("https://api.example.com")


def test_check_refresh_rejects_failed_task() -> None:
    # Given
    tasks = [{"name": f"task-{index}", "status": "ok"} for index in range(14)]
    tasks[3] = {"name": "task-3", "status": "error"}

    # When / Then
    with (
        patch("healthcheck.request_json", return_value=(200, {"status": "ok", "tasks": tasks}, 0.1)),
        pytest.raises(RuntimeError, match="refresh task failed"),
    ):
        check_refresh("https://api.example.com")


def test_check_endpoint_returns_success_for_populated_payload() -> None:
    # Given
    spec = EndpointSpec("/api/data", (("data",),))

    # When
    with patch("healthcheck.request_json", return_value=(200, {"data": [1]}, 0.1254)):
        result = check_endpoint("https://api.example.com", spec, max_latency=1, max_age_hours=30)

    # Then
    assert result == {
        "path": "/api/data",
        "status": "ok",
        "http_status": 200,
        "latency_seconds": 0.125,
    }


def test_check_endpoint_returns_error_for_http_status() -> None:
    # Given
    spec = EndpointSpec("/api/data", (("data",),))

    # When
    with patch("healthcheck.request_json", return_value=(502, {"error": "bad gateway"}, 0.1)):
        result = check_endpoint("https://api.example.com", spec, max_latency=1, max_age_hours=30)

    # Then
    assert result["status"] == "error"
    assert "HTTP 502" in str(result["error"])


def test_check_endpoint_returns_error_for_excessive_latency() -> None:
    # Given
    spec = EndpointSpec("/api/data", (("data",),))

    # When
    with patch("healthcheck.request_json", return_value=(200, {"data": [1]}, 1.5)):
        result = check_endpoint("https://api.example.com", spec, max_latency=1, max_age_hours=30)

    # Then
    assert result["status"] == "error"
    assert "latency 1.500s exceeded" in str(result["error"])


def test_check_endpoint_returns_error_for_transport_failure() -> None:
    # Given
    spec = EndpointSpec("/api/data", (("data",),))

    # When
    with patch("healthcheck.request_json", side_effect=URLError("offline")):
        result = check_endpoint("https://api.example.com", spec, max_latency=1, max_age_hours=30)

    # Then
    assert result["status"] == "error"
    assert "offline" in str(result["error"])


def test_check_endpoint_reports_fresh_status_age() -> None:
    # Given
    updated = datetime.now(timezone.utc) - timedelta(hours=1)
    spec = EndpointSpec("/api/status", (("last_updated",),))

    # When
    with patch("healthcheck.request_json", return_value=(200, {"last_updated": updated.isoformat()}, 0.1)):
        result = check_endpoint("https://api.example.com", spec, max_latency=1, max_age_hours=30)

    # Then
    assert result["status"] == "ok"
    assert 0.9 <= float(result["freshness_hours"]) <= 1.1


def test_run_rejects_relative_base_url_without_probing() -> None:
    # Given / When
    with patch("healthcheck.check_endpoint") as endpoint_check:
        report = run("api.example.com/", refresh=True, max_latency=1, max_age_hours=30)

    # Then
    assert report["status"] == "error"
    assert report["base_url"] == "api.example.com"
    assert report["endpoints"] == []
    endpoint_check.assert_not_called()


def test_run_collects_endpoint_failures() -> None:
    # Given
    failure = {"path": "/api/data", "status": "error", "error": "offline"}

    # When
    with patch("healthcheck.check_endpoint", return_value=failure):
        report = run("https://api.example.com", refresh=False, max_latency=1, max_age_hours=30)

    # Then
    assert report["status"] == "error"
    assert len(report["failures"]) == len(ENDPOINTS)


def test_run_records_successful_refresh() -> None:
    # Given
    refresh_result = {"status": "ok", "tasks": []}
    endpoint_result = {"path": "/api/data", "status": "ok"}

    # When
    with (
        patch("healthcheck.check_refresh", return_value=refresh_result),
        patch("healthcheck.check_endpoint", return_value=endpoint_result),
    ):
        report = run("https://api.example.com/", refresh=True, max_latency=1, max_age_hours=30)

    # Then
    assert report["status"] == "ok"
    assert report["refresh"] == refresh_result
    assert report["base_url"] == "https://api.example.com"


def test_main_writes_success_report(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    # Given
    output = tmp_path / "health.json"
    report = {"status": "ok"}
    argv = ["healthcheck.py", "--base-url", "https://api.example.com", "--output", str(output)]

    # When
    with patch.object(sys, "argv", argv), patch("healthcheck.run", return_value=report):
        exit_code = main()

    # Then
    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert json.loads(capsys.readouterr().out) == report


def test_main_returns_failure_exit_code(tmp_path) -> None:
    # Given
    output = tmp_path / "health.json"
    report = {"status": "error"}
    argv = ["healthcheck.py", "--output", str(output)]

    # When
    with patch.object(sys, "argv", argv), patch("healthcheck.run", return_value=report):
        exit_code = main()

    # Then
    assert exit_code == 1
