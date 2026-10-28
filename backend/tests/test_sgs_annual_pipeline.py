from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import monotonic

from fastapi.testclient import TestClient

import provider_http
from db import store
from db.refresh_run_models import (
    CycleId,
    RefreshRunStart,
    RefreshWorkItemSpec,
    RunId,
    WorkItemKey,
)
from db.refresh_runs import create_or_resume_run
from db.sgs_refresh_plan import (
    build_sgs_plan,
    freeze_sgs_plan,
    plan_digest,
    plan_item_task_name,
)
from main import app
from refresh_run_client import RefreshRunClientConfig, run_resumable_refresh


def test_annual_pib_v6_client_to_local_http_source_persistence_and_api(
    monkeypatch,
) -> None:
    class SgsHandler(BaseHTTPRequestHandler):
        requested_path = ""

        def do_GET(self) -> None:
            type(self).requested_path = self.path
            payload = (
                b'[{"data":"01/01/2024","valor":"11779250581628.30"},'
                b'{"data":"01/01/2025","valor":"12738565613881.10"}]'
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    plan = build_sgs_plan(date(2026, 10, 28), max_archive_units=0)
    unit = next(item for item in plan.units if item.series_code == 1207)
    assert unit.start_date == date(2020, 1, 1)
    assert unit.end_date == date(2026, 10, 28)
    legacy_first_seen = "2025-02-01T12:00:00+00:00"
    for reference, value in (
        ("2024-01-01", 11779250581628.3),
        ("2025-01-01", 12738565613881.1),
    ):
        vintage = store._vintage_row(
            "bcb_sgs", "pib", reference, {"valor": value},
            first_seen_at=legacy_first_seen,
        )
        store._execute(
            "INSERT INTO observation_vintages "
            "(provider, series_key, reference_period, dimensions_json, dimensions_hash, raw_value_json, "
            "source_published_at, first_seen_at, available_at, payload_hash, revision_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            vintage,
        )
    server = ThreadingHTTPServer(("127.0.0.1", 0), SgsHandler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local_url = f"http://127.0.0.1:{server.server_port}/sgs.json"
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda _url: local_url)

    run_id = RunId("sqlite-sgs-1207-annual")
    now = datetime.now(timezone.utc)
    task_name = plan_item_task_name(unit)
    create_or_resume_run(RefreshRunStart(
        run_id=run_id, cycle_id=CycleId(str(run_id)), force=True,
        manifest_version="daily-refresh-v6", derived_cutoff=None, macro_cutoff=None,
        items=(RefreshWorkItemSpec(WorkItemKey(task_name), task_name, 0),),
        now=now.isoformat(), lease_expires_at=(now + timedelta(minutes=30)).isoformat(),
    ))
    freeze_sgs_plan(str(run_id), plan, now)
    config = RefreshRunClientConfig(
        "http://testserver", str(run_id), str(run_id), "test-token", resume_existing=True,
        force=True, max_wait_seconds=30,
    )

    def request_json(url, method, _timeout, headers=None, body=None):
        response = client.request(
            method, url.removeprefix("http://testserver"), headers=headers, json=body,
        )
        return response.status_code, response.json(), monotonic() - started

    try:
        with TestClient(app) as client:
            started = monotonic()
            run_result = run_resumable_refresh(config, request_json)
            response = client.get("/api/atividade/pib")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert run_result.status == "ok"
    assert "dataInicial=01%2F01%2F2020" in SgsHandler.requested_path
    assert "dataFinal=28%2F10%2F2026" in SgsHandler.requested_path
    assert response.status_code == 200
    returned = response.json()["data"]
    assert returned == [
        {"data": "01/01/2024", "valor": "11779250581628.3"},
        {"data": "01/01/2025", "valor": "12738565613881.1"},
    ]

    persisted_unit = store._query(
        "SELECT contract_version, unit, frequency, start_date, end_date, status "
        "FROM sgs_refresh_plan_units WHERE run_id = ? AND unit_id = ?",
        (str(run_id), unit.unit_id),
    )[0]
    assert persisted_unit == {
        "contract_version": "acquisition-contract-v2", "unit": "brl", "frequency": "annual",
        "start_date": "2020-01-01", "end_date": "2026-10-28", "status": "reserved",
    }
    commit = store._query(
        "SELECT contract_version, canonical_value_sha256, captured_at, requested_start, "
        "requested_end, observed_count FROM sgs_unit_commits "
        "WHERE run_id = ? AND series_code = 1207", (str(run_id),),
    )[0]
    assert commit["contract_version"] == "acquisition-contract-v2"
    assert commit["canonical_value_sha256"]
    assert commit["requested_start"] == "2020-01-01"
    assert commit["requested_end"] == "2026-10-28"
    assert commit["observed_count"] == 2

    raw_vintages = store._query(
        "SELECT raw_value_json, first_seen_at, available_at, source_published_at "
        "FROM observation_vintages WHERE provider = 'bcb_sgs' AND series_key = 'pib' "
        "ORDER BY reference_period",
    )
    assert len(raw_vintages) == 2
    values = [json.loads(row["raw_value_json"]) for row in raw_vintages]
    assert [row["valor"] for row in values] == [
        11779250581628.3, 12738565613881.1
    ]
    assert all(row["source_published_at"] is None for row in raw_vintages)
    assert all(row["first_seen_at"] == legacy_first_seen for row in raw_vintages)
    assert all(row["available_at"] == legacy_first_seen for row in raw_vintages)
    assert store._query(
        "SELECT plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?", (str(run_id),)
    )[0]["plan_sha256"] == plan_digest(plan)
