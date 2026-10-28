"""Version-6 work-item retry decisions based on persisted typed categories.

Only known transport failures may retry a completed failed acquisition. Missing
or unknown categories (including old rows) are resolved conservatively rather
than re-downloaded to repair an uncertain outcome.
"""

from __future__ import annotations

from typing import Final

RETRYABLE_FAILED_CATEGORIES: Final = frozenset({
    "network_timeout",
    "network_reset",
    "temporary_server_error",
})

PERMANENT_FAILED_CATEGORIES: Final = frozenset({
    "invalid_schema",
    "semantic_conflict",
    "incomplete_coverage",
    "authentication_error",
    "configuration_error",
    "forbidden_host",
    # A complete but undecodable body is not evidence of a temporary outage.
    "envelope_error",
})


def failed_item_is_retryable(category: str | None, acquisitions: int, max_attempts: int) -> bool:
    """Permit retry only for recognized transient failures under the cap."""
    return category in RETRYABLE_FAILED_CATEGORIES and acquisitions < max_attempts


def failed_item_is_resolved(category: str | None, acquisitions: int, max_attempts: int) -> bool:
    """Missing/unknown and permanent categories resolve without more network."""
    return not failed_item_is_retryable(category, acquisitions, max_attempts)


def failed_work_item_is_retryable(
    task: str | None, category: str | None, acquisitions: int, attempts: int, max_attempts: int,
) -> bool:
    """Apply source-acquisition policy only to source tasks in the frozen manifest.

    Derived/phase callbacks retain bounded work-item retries; their claim attempts
    are not source acquisition attempts. ``task`` is persisted manifest metadata.
    """
    from refresh_run_scheduler import source_scope_for_task

    if task is not None and source_scope_for_task(task) is None:
        return attempts < max_attempts
    return failed_item_is_retryable(category, acquisitions, max_attempts)


def failed_work_item_is_resolved(
    task: str | None, category: str | None, acquisitions: int, attempts: int, max_attempts: int,
) -> bool:
    return not failed_work_item_is_retryable(task, category, acquisitions, attempts, max_attempts)
