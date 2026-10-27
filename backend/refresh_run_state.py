from datetime import datetime, timedelta

from db.refresh_run_models import (
    RefreshRunSnapshot,
    RefreshRunStart,
    RefreshWorkItemSpec,
    RunId,
)
from db.refresh_runs import create_or_resume_run, read_run
from db.sgs_refresh_plan import read_sgs_plan
from refresh import LEASE_SECONDS


class RefreshRunNotFoundError(LookupError):
    def __init__(self, run_id: RunId) -> None:
        self.run_id = run_id
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"refresh run {self.run_id} was not found"


def lease_expiry(now: datetime) -> str:
    return (now + timedelta(seconds=LEASE_SECONDS)).isoformat()


def get_refresh_run(run_id: RunId) -> RefreshRunSnapshot:
    snapshot = read_run(run_id)
    if snapshot is None:
        raise RefreshRunNotFoundError(run_id)
    return snapshot


def resume_refresh_run(snapshot: RefreshRunSnapshot, now: datetime) -> RefreshRunSnapshot:
    run = snapshot.run
    return create_or_resume_run(
        RefreshRunStart(
            run_id=run.run_id,
            cycle_id=run.cycle_id,
            force=run.force,
            manifest_version=run.manifest_version,
            derived_cutoff=run.derived_cutoff,
            macro_cutoff=run.macro_cutoff,
            items=tuple(
                RefreshWorkItemSpec(item.item_key, item.task, item.task_order)
                for item in snapshot.items
            ),
            now=now.isoformat(),
            lease_expires_at=lease_expiry(now),
            sgs_plan=read_sgs_plan(str(run.run_id)) if run.manifest_version == "daily-refresh-v6" else None,
        )
    )
