"""Trusted in-process context and durable lifecycle for one refresh claim."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from db import store
from db.refresh_run_models import ClaimToken, RefreshWorkItemRecord, RunId, WorkItemKey


@dataclass(frozen=True, slots=True)
class RefreshAttemptContext:
    attempt_id: str
    run_id: RunId
    item_key: WorkItemKey
    attempt_number: int
    generation: int
    claim_token: ClaimToken
    expires_at: str


_active: ContextVar[RefreshAttemptContext | None] = ContextVar("refresh_attempt", default=None)


def current_refresh_attempt() -> RefreshAttemptContext | None:
    return _active.get()


def install_refresh_attempt(context: RefreshAttemptContext) -> Token[RefreshAttemptContext | None]:
    return _active.set(context)


def reset_refresh_attempt(token: Token[RefreshAttemptContext | None]) -> None:
    _active.reset(token)


def begin_refresh_attempt(context: RefreshAttemptContext, now: str) -> None:
    store.record_refresh_attempt_started(context, now)


def record_refresh_attempt_stage(stage: str, outcome: str = "observed", detail_code: str | None = None) -> None:
    context = current_refresh_attempt()
    if context is not None:
        store.record_refresh_attempt_milestone(context.attempt_id, stage, outcome, detail_code)


def finish_refresh_attempt(
    context: RefreshAttemptContext,
    outcome: str,
    *,
    error_code: str | None = None,
    committed_count: int | None = None,
    source_data_date: str | None = None,
) -> None:
    store.finish_refresh_attempt(context.attempt_id, outcome, error_code, committed_count, source_data_date)


def new_refresh_attempt(claim: RefreshWorkItemRecord) -> RefreshAttemptContext:
    return RefreshAttemptContext(
        str(uuid4()),
        RunId(str(claim.run_id)),
        WorkItemKey(str(claim.item_key)),
        claim.attempt,
        claim.attempt,
        ClaimToken(str(claim.claim_token)),
        str(claim.claim_expires_at),
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
