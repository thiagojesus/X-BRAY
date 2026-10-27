from collections.abc import Mapping
from datetime import datetime, timezone


def source_scope_for_task(task: str) -> str | None:
    if task.startswith(("sgs_", "ipca_grupos")):
        return "bcb_sgs:primary:primary"
    for prefix, provider in (
        ("tesouro", "tesouro"), ("focus", "focus"), ("anbima", "anbima"),
        ("b3", "b3"), ("ibge_sidra", "ibge_sidra"),
    ):
        if task == prefix or task.startswith(f"{prefix}_"):
            return f"{provider}:primary:primary"
    return None


def provider_eligibility(
    state: Mapping[str, object] | None, now: datetime,
) -> tuple[bool, str | None, str | None]:
    if state is None:
        return True, None, None
    retry_at = _timestamp(state.get("retry_not_before"))
    opened_until = _timestamp(state.get("opened_until"))
    lease_until = _timestamp(state.get("probe_lease_until"))
    state_name = state.get("state")
    next_at = max((value for value in (retry_at, opened_until) if value is not None), default=None)
    if state_name == "closed":
        return True, None, None
    if lease_until is not None and lease_until > now:
        return False, lease_until.isoformat(), "probe_lease_active"
    if next_at is not None and next_at > now:
        return False, next_at.isoformat(), "publisher_cooldown"
    # An expired open/half-open state is eligible for the exclusive network
    # probe. This is a read-only peek; the lease is acquired only at dispatch.
    return True, None, None


def item_eligibility(task: str, states: Mapping[str, Mapping[str, object]], now: datetime):
    scope = source_scope_for_task(task)
    return provider_eligibility(states.get(scope) if scope else None, now)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
