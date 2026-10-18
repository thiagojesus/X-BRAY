from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NewType

RunId = NewType("RunId", str)
CycleId = NewType("CycleId", str)
WorkItemKey = NewType("WorkItemKey", str)
ClaimToken = NewType("ClaimToken", str)

MAX_WORK_ITEM_ATTEMPTS: Final = 2


class RefreshRunState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class RefreshRunFinalState(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class RefreshRunCutoffPhase(StrEnum):
    DERIVED = "derived"
    MACRO_SCORE = "macro_score"


class RefreshWorkItemStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class RefreshWorkItemOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class RefreshWorkItemSpec:
    item_key: WorkItemKey
    task: str
    task_order: int


@dataclass(frozen=True, slots=True)
class RefreshRunStart:
    run_id: RunId
    cycle_id: CycleId
    force: bool
    manifest_version: str
    derived_cutoff: str | None
    macro_cutoff: str | None
    items: tuple[RefreshWorkItemSpec, ...]
    now: str
    lease_expires_at: str


@dataclass(frozen=True, slots=True)
class RefreshRunRecord:
    run_id: RunId
    cycle_id: CycleId
    state: RefreshRunState
    force: bool
    manifest_version: str
    derived_cutoff: str | None
    macro_cutoff: str | None
    created_at: str
    updated_at: str
    finished_at: str | None
    final_result: str | None


@dataclass(frozen=True, slots=True)
class RefreshWorkItemRecord:
    run_id: RunId
    item_key: WorkItemKey
    task: str
    task_order: int
    status: RefreshWorkItemStatus
    attempt: int
    claim_token: ClaimToken | None
    claim_expires_at: str | None
    claimed_at: str | None
    created_at: str
    updated_at: str
    finished_at: str | None
    series_updated: int
    record_count: int
    error_code: str | None


@dataclass(frozen=True, slots=True)
class RefreshRunSnapshot:
    run: RefreshRunRecord
    items: tuple[RefreshWorkItemRecord, ...]
    lease_expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class RefreshWorkItemClaimRequest:
    run_id: RunId
    now: str
    claim_expires_at: str
    expected_item_key: WorkItemKey | None = None


@dataclass(frozen=True, slots=True)
class RefreshWorkItemCompletion:
    run_id: RunId
    item_key: WorkItemKey
    claim_token: ClaimToken
    status: RefreshWorkItemOutcome
    finished_at: str
    series_updated: int
    record_count: int
    error_code: str | None


@dataclass(frozen=True, slots=True)
class RefreshRunFinalization:
    run_id: RunId
    state: RefreshRunFinalState
    final_result: str
    finished_at: str
    last_successful_update: str | None = None


@dataclass(frozen=True, slots=True)
class RefreshRunCutoffUpdate:
    run_id: RunId
    phase: RefreshRunCutoffPhase
    cutoff: str
    now: str
    lease_expires_at: str


class RefreshLeaseConflictError(RuntimeError):
    __slots__ = ("owner", "run_id")

    def __init__(self, run_id: RunId, owner: RunId) -> None:
        super().__init__()
        self.run_id = run_id
        self.owner = owner

    def __str__(self) -> str:
        return f"daily refresh lease is owned by {self.owner}; cannot start {self.run_id}"


class RefreshRunIdempotencyError(RuntimeError):
    __slots__ = ("run_id",)

    def __init__(self, run_id: RunId) -> None:
        super().__init__()
        self.run_id = run_id

    def __str__(self) -> str:
        return f"run id {self.run_id} was already used with a different refresh manifest"


class RefreshRunCutoffOrderError(RuntimeError):
    __slots__ = ("run_id",)

    def __init__(self, run_id: RunId) -> None:
        super().__init__()
        self.run_id = run_id

    def __str__(self) -> str:
        return f"Macro Score cutoff requires a persisted derived cutoff for run {self.run_id}"


class RefreshRunInputError(ValueError):
    __slots__ = ("field", "value")

    def __init__(self, field: str, value: str) -> None:
        super().__init__()
        self.field = field
        self.value = value

    def __str__(self) -> str:
        return f"{self.field} must be a timezone-aware ISO timestamp: {self.value!r}"


class RefreshPersistenceError(RuntimeError):
    __slots__ = ("run_id",)

    def __init__(self, run_id: RunId) -> None:
        super().__init__()
        self.run_id = run_id

    def __str__(self) -> str:
        return f"refresh-run persistence invariant failed for {self.run_id}"
