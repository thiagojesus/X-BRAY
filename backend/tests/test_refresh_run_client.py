import json

import pytest

from healthcheck import HealthcheckProbeError
from healthcheck_payloads import JsonObject, JsonValue
from refresh_run_client import (
    RefreshRunClientConfig,
    RefreshRunClientError,
    run_resumable_refresh,
)


class LostResponseError(HealthcheckProbeError):
    pass


class StatusUnavailableError(RuntimeError):
    pass


def _run_snapshot(state: str, item_status: str) -> JsonObject:
    return {
        "run": {
            "run_id": "client-run",
            "cycle_id": "client-run",
            "state": state,
            "force": False,
            "manifest_version": "daily-refresh-v1",
            "final_result": None,
        },
        "items": [
            {
                "item_key": "source-one",
                "status": item_status,
                "attempt": 1,
                "claim_expires_at": None,
            }
        ],
    }


def test_lost_advance_response_is_reconciled_before_any_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    current = _run_snapshot("running", "pending")
    calls: list[tuple[str, str, JsonValue | None]] = []
    terminal: JsonObject = {
        "run": {
            "run_id": "client-run",
            "cycle_id": "client-run",
            "state": "succeeded",
            "force": False,
            "manifest_version": "daily-refresh-v1",
            "final_result": json.dumps({
                "status": "ok",
                "tasks": [{"name": "source-one", "status": "ok", "error_code": None}],
            }),
        },
        "items": [
            {
                "item_key": "source-one",
                "status": "succeeded",
                "attempt": 1,
                "claim_expires_at": None,
            }
        ],
    }

    def request(
        url: str,
        method: str,
        _timeout: float,
        _headers: dict[str, str] | None = None,
        body: JsonValue | None = None,
    ) -> tuple[int, JsonValue, float]:
        calls.append((url, method, body))
        if method == "POST" and url.endswith("/api/refresh-runs"):
            return 200, current, 0.1
        if method == "POST" and url.endswith("/advance"):
            current["items"] = [
                {"item_key": "source-one", "status": "succeeded", "attempt": 1, "claim_expires_at": None}
            ]
            raise LostResponseError("response lost after commit")
        if method == "GET":
            return 200, current, 0.1
        if method == "POST" and url.endswith("/finalize"):
            return 200, terminal, 0.1
        pytest.fail(f"unexpected request: {method} {url}")

    monkeypatch.setattr("refresh_run_client.time.sleep", lambda _seconds: None)

    # When
    result = run_resumable_refresh(
        RefreshRunClientConfig("https://api.example.com", "client-run", "client-run", "token"),
        request,
    )

    # Then
    advances = [call for call in calls if call[1] == "POST" and call[0].endswith("/advance")]
    assert len(advances) == 1
    assert advances[0][2] == {"expected_item_key": "source-one"}
    assert result.status == "ok"
    assert result.tasks == [{"name": "source-one", "status": "ok", "error_code": None}]


def test_unavailable_status_after_lost_advance_does_not_retry_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    mutation_count = 0

    def request(
        url: str,
        method: str,
        _timeout: float,
        _headers: dict[str, str] | None = None,
        _body: JsonValue | None = None,
    ) -> tuple[int, JsonValue, float]:
        nonlocal mutation_count
        if method == "POST" and url.endswith("/api/refresh-runs"):
            return 200, _run_snapshot("running", "pending"), 0.1
        if method == "POST" and url.endswith("/advance"):
            mutation_count += 1
            raise LostResponseError("response lost")
        raise StatusUnavailableError("status unavailable")

    monkeypatch.setattr("refresh_run_client.time.sleep", lambda _seconds: None)

    # When
    with pytest.raises(RefreshRunClientError, match="could not reconcile"):
        run_resumable_refresh(
            RefreshRunClientConfig("https://api.example.com", "client-run", "client-run", "token"),
            request,
        )

    # Then
    assert mutation_count == 1


def test_resume_existing_checks_persisted_run_before_idempotent_start() -> None:
    terminal = _run_snapshot("succeeded", "succeeded")
    terminal["run"]["final_result"] = json.dumps(
        {"status": "ok", "tasks": [{"name": "source-one", "status": "ok", "error_code": None}]}
    )
    calls: list[tuple[str, str]] = []

    def request(
        url: str,
        method: str,
        _timeout: float,
        _headers: dict[str, str] | None = None,
        _body: JsonValue | None = None,
    ) -> tuple[int, JsonValue, float]:
        calls.append((method, url))
        if method == "GET":
            return 200, terminal, 0.1
        if method == "POST" and url.endswith("/api/refresh-runs"):
            return 200, terminal, 0.1
        pytest.fail(f"unexpected request: {method} {url}")

    result = run_resumable_refresh(
        RefreshRunClientConfig(
            "https://api.example.com",
            "client-run",
            "client-run",
            "token",
            resume_existing=True,
        ),
        request,
    )

    assert [method for method, _url in calls] == ["GET", "POST"]
    assert result.status == "ok"


def test_resume_existing_fails_closed_when_run_is_missing() -> None:
    calls = 0

    def request(
        _url: str,
        method: str,
        _timeout: float,
        _headers: dict[str, str] | None = None,
        _body: JsonValue | None = None,
    ) -> tuple[int, JsonValue, float]:
        nonlocal calls
        calls += 1
        assert method == "GET"
        return 404, {}, 0.1

    with pytest.raises(RefreshRunClientError, match="refusing to create a new run"):
        run_resumable_refresh(
            RefreshRunClientConfig(
                "https://api.example.com",
                "missing-run",
                "missing-run",
                "token",
                resume_existing=True,
            ),
            request,
        )

    assert calls == 1


def test_lost_start_response_reuses_the_same_run_id_without_second_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    current = _run_snapshot("running", "pending")
    start_count = 0
    terminal: JsonObject = {
        "run": {
            "run_id": "client-run",
            "cycle_id": "client-run",
            "state": "succeeded",
            "force": False,
            "manifest_version": "daily-refresh-v1",
            "final_result": json.dumps({
                "status": "ok",
                "tasks": [{"name": "source-one", "status": "ok", "error_code": None}],
            }),
        },
        "items": [
            {
                "item_key": "source-one",
                "status": "succeeded",
                "attempt": 1,
                "claim_expires_at": None,
            }
        ],
    }

    def request(
        url: str,
        method: str,
        _timeout: float,
        _headers: dict[str, str] | None = None,
        _body: JsonValue | None = None,
    ) -> tuple[int, JsonValue, float]:
        nonlocal start_count
        if method == "POST" and url.endswith("/api/refresh-runs"):
            start_count += 1
            raise LostResponseError("start response lost after creation")
        if method == "GET":
            return 200, current, 0.1
        if method == "POST" and url.endswith("/advance"):
            current["items"] = [
                {"item_key": "source-one", "status": "succeeded", "attempt": 1, "claim_expires_at": None}
            ]
            return 200, current, 0.1
        if method == "POST" and url.endswith("/finalize"):
            return 200, terminal, 0.1
        pytest.fail(f"unexpected request: {method} {url}")

    monkeypatch.setattr("refresh_run_client.time.sleep", lambda _seconds: None)

    # When
    result = run_resumable_refresh(
        RefreshRunClientConfig("https://api.example.com", "client-run", "client-run", "token"),
        request,
    )

    # Then
    assert start_count == 1
    assert result.status == "ok"
