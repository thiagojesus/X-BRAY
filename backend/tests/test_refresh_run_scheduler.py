from datetime import datetime, timedelta, timezone

from refresh_run_scheduler import item_eligibility, source_scope_for_task


def test_sgs_children_share_read_only_publisher_eligibility_scope() -> None:
    assert source_scope_for_task("sgs_juros_w432_2015-01-01_2015-12-31") == "bcb_sgs:primary:primary"
    assert source_scope_for_task("tesouro") == "tesouro:primary:primary"
    assert source_scope_for_task("derived_snapshots") is None


def test_future_retry_after_defers_without_claiming_or_leasing() -> None:
    now = datetime(2026, 10, 27, 13, 5, tzinfo=timezone.utc)
    future = (now + timedelta(hours=1)).isoformat()
    eligible, next_at, reason = item_eligibility(
        "sgs_juros_w432",
        {"bcb_sgs:primary:primary": {"state": "open", "retry_not_before": future}},
        now,
    )
    assert (eligible, next_at, reason) == (False, future, "publisher_cooldown")


def test_expired_open_circuit_is_eligible_for_single_dispatch_gate_probe() -> None:
    now = datetime(2026, 10, 27, 14, 51, tzinfo=timezone.utc)
    elapsed = (now - timedelta(hours=1, minutes=46)).isoformat()
    eligible, next_at, reason = item_eligibility(
        "sgs_juros_w432",
        {"bcb_sgs:primary:primary": {"state": "open", "opened_until": elapsed}},
        now,
    )
    assert eligible and next_at is None and reason is None
