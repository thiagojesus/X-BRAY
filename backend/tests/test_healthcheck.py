import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from healthcheck import (
    ENDPOINTS,
    EndpointSpec,
    JsonObject,
    JsonValue,
    check_endpoint,
    freshness_hours,
    run,
    validate_payload,
)


def test_manifest_covers_every_public_page_dataset() -> None:
    paths = {spec.path for spec in ENDPOINTS}
    assert paths == {
        "/api/status",
        "/api/juros",
        "/api/inflacao",
        "/api/ipca-decomposicao/tudo",
        "/api/atividade",
        "/api/atividade/sidra",
        "/api/cambio",
        "/api/titulos",
        "/api/focus",
        "/api/complementares",
        "/api/tesouro-direto",
        "/api/curvas-di",
        "/api/fiscal",
        "/api/credito",
    }
    curves = next(spec for spec in ENDPOINTS if spec.path == "/api/curvas-di")
    assert curves.required_paths == (("dates",), ("curves",))
    fiscal = next(spec for spec in ENDPOINTS if spec.path == "/api/fiscal")
    assert fiscal.required_paths == (("data",), ("metadata",))
    credit = next(spec for spec in ENDPOINTS if spec.path == "/api/credito")
    assert credit.required_paths == (("data",), ("metadata",))
    sidra = next(spec for spec in ENDPOINTS if spec.path == "/api/atividade/sidra")
    assert sidra.required_paths == (("data",), ("metadata",))


def test_payload_validation_rejects_empty_nested_series() -> None:
    with pytest.raises(ValueError, match="empty JSON path: data"):
        validate_payload({"data": {"selic": []}}, (("data",),))


def test_payload_validation_accepts_populated_nested_series() -> None:
    validate_payload({"data": {"selic": [{"data": "01/01/2026", "valor": "1"}]}}, (("data",),))


def test_sidra_healthcheck_accepts_optional_null_metadata_fields() -> None:
    # Given
    sidra = next(spec for spec in ENDPOINTS if spec.path == "/api/atividade/sidra")
    payload: JsonObject = {
        "data": {"massa_rendimento_real": [{"reference_period": "202507", "value": 100.0}]},
        "metadata": {
            "massa_rendimento_real": {
                "table": {"id": 6392},
                "classification_id": None,
                "category_id": None,
                "classification": None,
                "category": None,
            }
        },
    }

    # When
    with patch("healthcheck.request_json", return_value=(200, payload, 0.1)):
        result = check_endpoint("https://api.example.com", sidra, max_latency=1, max_age_hours=30)

    # Then
    assert result == {
        "path": "/api/atividade/sidra",
        "status": "ok",
        "http_status": 200,
        "latency_seconds": 0.1,
    }


@pytest.mark.parametrize("metadata", [None, {}, {"classification": None}])
def test_payload_validation_rejects_metadata_without_content(metadata: JsonValue) -> None:
    # Given / When / Then
    with pytest.raises(ValueError, match="empty JSON path: metadata"):
        validate_payload({"metadata": metadata}, (("metadata",),))


def test_payload_validation_rejects_empty_metadata_sibling_with_optional_null() -> None:
    # Given
    payload: JsonObject = {
        "metadata": {
            "table": {"id": 6392},
            "classification": None,
            "label": "",
        }
    }

    # When / Then
    with pytest.raises(ValueError, match="empty JSON path: metadata"):
        validate_payload(payload, (("metadata",),))


def test_freshness_hours_accepts_utc_timestamp() -> None:
    updated = datetime.now(timezone.utc) - timedelta(hours=2)
    age = freshness_hours(updated.isoformat())
    assert 1.9 <= age <= 2.1


def test_status_check_rejects_stale_refresh_timestamp() -> None:
    stale = datetime.now(timezone.utc) - timedelta(hours=31)
    with patch("healthcheck.request_json", return_value=(200, {"last_updated": stale.isoformat()}, 0.1)):
        result = check_endpoint(
            "https://api.example.com",
            EndpointSpec("/api/status", (("last_updated",),)),
            max_latency=5,
            max_age_hours=30,
        )
    assert result["status"] == "error"
    assert "last_updated age" in str(result["error"])


def test_endpoint_probes_continue_when_refresh_fails() -> None:
    healthy = {"path": "/api/status", "status": "ok", "http_status": 200, "latency_seconds": 0.1}
    with (
        patch("healthcheck.check_refresh", side_effect=RuntimeError("provider failed")),
        patch("healthcheck.check_endpoint", return_value=healthy) as endpoint_check,
    ):
        report = run("https://api.example.com", refresh=True, max_latency=5, max_age_hours=30)
    assert report["status"] == "error"
    assert report["refresh"]["status"] == "error"
    assert report["refresh"]["error"] == "provider failed"
    assert report["refresh"]["acquisition_diagnostics"]["status"] == "not_recorded"
    assert endpoint_check.call_count == len(ENDPOINTS)


@pytest.mark.parametrize(
    ("summary_run_id", "expected_diag_status"),
    [("requested-run", "recorded"), ("other-run", "run_mismatch"), (None, "not_recorded")],
)
def test_partial_healthcheck_keeps_failure_and_sanitizes_run_bound_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    summary_run_id: str | None,
    expected_diag_status: str,
) -> None:
    from acquisition_diagnostics import SCHEMA_VERSION

    token = "test-token"
    authorization = "Bearer " + token
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", token)
    monkeypatch.setenv("XBRAY_REFRESH_RUN_ID", "requested-run")
    monkeypatch.setenv("XBRAY_RESUME_EXISTING", "true")
    calls: list[str] = []

    def fake_request(
        url: str, method: str, timeout: int, headers: dict[str, str] | None = None,
        body: JsonObject | None = None,
    ) -> tuple[int, JsonValue, float]:
        calls.append(f"{method} {url}")
        if method == "GET" and url.endswith("/api/refresh-runs/requested-run"):
            assert headers == {"Authorization": authorization}
        elif method == "POST" and url.endswith("/api/refresh-runs"):
            assert headers == {"Authorization": authorization}
            assert body is not None and body["run_id"] == "requested-run"
        else:
            assert method == "GET"
            assert url.endswith("/api/refresh-status") or any(url.endswith(spec.path) for spec in ENDPOINTS)
            if not url.endswith("/api/refresh-status"):
                spec = next(spec for spec in ENDPOINTS if url.endswith(spec.path))
                endpoint_payload: JsonObject = {}
                for path in spec.required_paths:
                    current = endpoint_payload
                    for key in path[:-1]:
                        child = current.get(key)
                        if not isinstance(child, dict):
                            child = {}
                            current[key] = child
                        current = child
                    current[path[-1]] = (
                        datetime.now(timezone.utc).isoformat()
                        if path == ("last_updated",) else [{"synthetic": "value"}]
                    )
                return 200, endpoint_payload, 0.01
        if url.endswith(("/api/refresh-runs/requested-run", "/api/refresh-runs")):
            return 200, {
                "run": {
                    "run_id": "requested-run", "cycle_id": "requested-run", "state": "partial",
                    "force": False, "manifest_version": "daily-refresh-v6",
                    "final_result": json.dumps({
                        "status": "partial", "tasks": [{
                            "name": "b3_curve", "status": "error", "error_code": "RuntimeError",
                        }],
                    }),
                },
                "items": [],
            }, 0.01
        summary: JsonObject | None = None
        if summary_run_id is not None:
            summary = {
                "schema_version": SCHEMA_VERSION,
                "run_id": summary_run_id,
                "status": "recorded",
                "items": [{
                    "source": "b3", "extraction_id": "extract-safe", "task_key": "b3_curve",
                    "status": "network_error", "diagnostics_status": "recorded",
                    "error_category": "network_timeout", "legacy_error_code": "unknown",
                    "request_context": {
                        "provider": "b3", "trade_date": "2026-10-29",
                        "file_name": "SPRD261029.zip", "local_attempt": 2,
                    },
                    "requests": [{
                        "failure_code": "read_timeout", "dispatch_sequence": 2,
                        "started_at": "2026-10-29T00:00:00+00:00",
                        "finished_at": "2026-10-29T00:00:01+00:00",
                        "response": {"status_code": None, "content_type": "unknown", "body_kind": "unknown", "bytes_received": None},
                        "url": "https://private.invalid/?token=LEAK_SENTINEL",
                    }],
                }],
                "truncated_items": 0,
                "truncated_rows": 0,
                "authorization": "LEAK_SENTINEL",
            }
        return 200, {
            "overall_status": "partial",
            "tasks": [{"last_success": "2026-10-28T00:00:00+00:00", "freshness": "ok"}],
            "acquisition_diagnostics": summary,
        }, 0.01

    with (
        patch("healthcheck.request_json", side_effect=fake_request),
    ):
        report = run("https://api.example.com", refresh=True, max_latency=5, max_age_hours=30)

    assert report["status"] == "error"
    assert report["failures"] == ["refresh run completed partially: b3_curve (RuntimeError)"]
    refresh = report["refresh"]
    assert refresh["status"] == "error"
    diagnostics = refresh["acquisition_diagnostics"]
    assert diagnostics["schema_version"] == SCHEMA_VERSION
    assert diagnostics["run_id"] == "requested-run"
    assert diagnostics["status"] == expected_diag_status
    assert "LEAK_SENTINEL" not in json.dumps(report)
    assert calls == [
        "GET https://api.example.com/api/refresh-runs/requested-run",
        "POST https://api.example.com/api/refresh-runs",
        "GET https://api.example.com/api/refresh-status",
        *(f"GET https://api.example.com{spec.path}" for spec in ENDPOINTS),
    ]
    assert all(endpoint["status"] == "ok" and endpoint["http_status"] == 200 for endpoint in report["endpoints"])
