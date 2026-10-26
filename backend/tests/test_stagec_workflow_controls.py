from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_manual_canary_is_dispatch_only_guarded_and_dry_run_by_default() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/source-canary.yml").read_text(), Loader=yaml.BaseLoader
    )
    job = workflow["jobs"]["canary-control"]
    steps = job["steps"]
    dispatch = workflow["on"]["workflow_dispatch"]
    assert "schedule" not in workflow["on"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert dispatch["inputs"]["dry_run"]["default"] == "true"
    assert dispatch["inputs"]["dry_run"]["type"] == "boolean"
    assert steps[0]["env"] == {"API_URL": "${{ secrets.XBRAY_API_URL }}"}
    request_step = steps[1]
    assert request_step["env"]["API_URL"] == "${{ secrets.XBRAY_API_URL }}"
    assert request_step["env"]["API_TOKEN"] == "${{ secrets.XBRAY_REFRESH_TOKEN }}"
    assert all("${{ inputs." not in step.get("run", "") for step in steps)
    assert "${{ secrets." not in "\n".join(step.get("run", "") for step in steps)
    assert "API host rejected" in request_step["run"]
    assert "xbray-api.vercel.app" in request_step["run"]
    assert "Authorization" in request_step["run"]


def test_weekly_audit_schedule_and_attention_gate_remain_enabled() -> None:
    workflow = (ROOT / ".github/workflows/source-audit-weekly.yml").read_text()
    assert 'cron: "0 12 * * 1"' in workflow
    assert "report.needs_attention !== true" in workflow
    assert "state: 'open'" in workflow
    assert "Candidate delivery never clears a primary-source incident." in workflow
