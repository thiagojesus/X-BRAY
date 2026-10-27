from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import local
from unittest.mock import MagicMock

import pytest

from db import store
from ingestion_policy import (
    ErrorCategory,
    RetryBudget,
    RetryBudgetExceeded,
    RetryProfile,
    classify_exception,
    install_retry_budget,
    reset_retry_budget,
    retry_profile,
)
from provider_hosts import ProviderHostRejectedError
from provider_http import get_provider_response


def test_failure_taxonomy_does_not_guess_unknown_errors_are_network_failures() -> None:
    assert classify_exception(RuntimeError("unexpected programming defect")) is ErrorCategory.INTERNAL_ERROR
    assert classify_exception(TimeoutError("deadline")) is ErrorCategory.NETWORK_TIMEOUT
    assert classify_exception(ProviderHostRejectedError("not-approved.invalid")) is ErrorCategory.FORBIDDEN_HOST


def test_provider_profiles_are_finite_and_preserve_stricter_attempt_caps() -> None:
    annual = retry_profile("bcb_sgs", {"window_policy": "calendar-year-v1"})
    assert annual.deadline_seconds == 90
    assert annual.request_timeout_seconds == 20
    assert retry_profile("bcb_sgs", {}).deadline_seconds == 220
    assert retry_profile("focus", {}).deadline_seconds == 45
    assert retry_profile("ibge_sidra", {}).max_requests == 3
    assert retry_profile("b3", {"days": 30}).max_requests == 64
    assert retry_profile("anbima", {}).max_requests == 3
    assert retry_profile("tesouro", {}).max_requests == 2


def test_child_unit_cannot_reset_its_budget_and_root_is_narrowed_to_plan() -> None:
    profile = retry_profile("ibge_sidra", {})
    budget = RetryBudget(profile)
    budget.set_request_limit(3)
    budget.set_unit_limit("sidra:all", 3)
    token = budget.enter_unit("sidra:all")
    try:
        budget.reserve_request()
        budget.reserve_request()
        budget.reserve_request()
        with pytest.raises(RetryBudgetExceeded):
            budget.reserve_request()
        budget.set_unit_limit("sidra:all", 3)
        assert budget.requests == 3
    finally:
        budget.leave_unit(token)


def test_budget_caps_actual_requests_and_counts_retries_separately_from_redirects() -> None:
    budget = RetryBudget(RetryProfile("bcb_sgs", 3, 20, 10))
    assert budget.reserve_request() > 0
    assert budget.reserve_request(retry=True) > 0
    assert budget.reserve_request() > 0
    assert (budget.requests, budget.retries) == (3, 1)
    with pytest.raises(RetryBudgetExceeded):
        budget.reserve_request()


@pytest.mark.parametrize(("source", "cap"), [("bcb_sgs", 3), ("tesouro", 2)])
def test_half_open_probe_preserves_provider_hop_cap_without_increasing_it(source, cap) -> None:
    budget = RetryBudget(retry_profile(source, {}))
    budget.restrict_to_probe()
    assert budget.half_open_probe is True
    for _ in range(cap):
        budget.reserve_request()
    with pytest.raises(RetryBudgetExceeded):
        budget.reserve_request()
    assert budget.requests == cap
    assert budget.profile.max_requests == cap


def test_retry_after_seconds_http_date_invalid_and_negative_are_explicit() -> None:
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    budget = RetryBudget(RetryProfile("focus", 3, 45, 30))
    assert budget.retry_after("3600", now=now) == now + timedelta(hours=1)
    assert budget.retry_after("Mon, 26 Oct 2026 13:00:00 GMT", now=now) == now + timedelta(hours=1)
    assert budget.retry_after("not-a-date", now=now) is None
    assert budget.retry_after("-1", now=now) is None
    assert budget.retry_after("0", now=now) is None


def test_deferred_retry_advice_never_shortens_an_existing_cooldown() -> None:
    budget = RetryBudget(RetryProfile("tesouro", 2, 90, 90))
    now = datetime.now(timezone.utc)
    long_advice = now + timedelta(hours=1)
    short_advice = now + timedelta(seconds=30)
    assert budget.defer(long_advice).retry_not_before == long_advice
    assert budget.defer(short_advice).retry_not_before == long_advice


def test_validated_provider_success_does_not_clear_future_retry_after(isolated_sqlite_policy_db) -> None:
    now = datetime.now(timezone.utc)
    admitted, _, _, generation = store.claim_source_access_probe(
        "tesouro:primary:primary", (now - timedelta(minutes=2)).isoformat(),
        (now - timedelta(minutes=1)).isoformat(),
    )
    assert admitted
    retry_at = now + timedelta(hours=1)
    store.record_source_access_failure(
        "tesouro:primary:primary", now.isoformat(), retry_at.isoformat(),
        retry_at.isoformat(), rate_limited=True,
    )
    store.record_source_access_success("tesouro:primary:primary", now.isoformat(), generation)
    state = store.get_source_access_state("tesouro:primary:primary")
    assert state is not None
    assert state["state"] == "open"
    assert state["retry_not_before"] == retry_at.isoformat()


def test_stale_success_ticket_preserves_entire_newer_breaker_state(isolated_sqlite_policy_db) -> None:
    scope = "bcb_sgs:primary:causal"
    instant = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    admitted, _, _, old_generation = store.claim_source_access_probe(
        scope, instant.isoformat(), (instant + timedelta(minutes=1)).isoformat(),
    )
    assert admitted
    later = instant + timedelta(seconds=1)
    retry_at = later + timedelta(hours=1)
    store.record_source_access_failure(
        scope, later.isoformat(), (later + timedelta(minutes=5)).isoformat(),
        retry_at.isoformat(), rate_limited=True,
    )
    before = store._query("SELECT * FROM source_access_state WHERE scope_id = ?", (scope,))[0]

    assert not store.record_source_access_success(scope, instant.isoformat(), old_generation)
    after = store._query("SELECT * FROM source_access_state WHERE scope_id = ?", (scope,))[0]
    assert after == before


def test_same_timestamp_new_probe_generation_fences_expired_worker(isolated_sqlite_policy_db) -> None:
    scope = "bcb_sgs:primary:same-time"
    instant = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    store.record_source_access_failure(
        scope, instant.isoformat(), (instant + timedelta(seconds=1)).isoformat(),
        None, rate_limited=False, failure_count=3,
    )
    due = instant + timedelta(minutes=5)
    old_claimed, _, _, old_ticket = store.claim_source_access_probe(
        scope, due.isoformat(), (due + timedelta(seconds=30)).isoformat(),
    )
    assert old_claimed
    expired = due + timedelta(seconds=31)
    new_claimed, _, _, new_ticket = store.claim_source_access_probe(
        scope, expired.isoformat(), (expired + timedelta(seconds=30)).isoformat(),
    )
    assert new_claimed and new_ticket > old_ticket
    prior = store._query("SELECT * FROM source_access_state WHERE scope_id = ?", (scope,))[0]
    assert not store.record_source_access_success(scope, due.isoformat(), old_ticket)
    assert store._query("SELECT * FROM source_access_state WHERE scope_id = ?", (scope,))[0] == prior

    assert store.record_source_access_success(scope, expired.isoformat(), new_ticket)
    closed = store._query("SELECT * FROM source_access_state WHERE scope_id = ?", (scope,))[0]
    assert closed["state"] == "closed"
    assert closed["probe_lease_until"] is None
    assert closed["updated_at"] >= prior["updated_at"]
    assert store.record_source_access_success(scope, due.isoformat(), new_ticket)
    assert store._query("SELECT * FROM source_access_state WHERE scope_id = ?", (scope,))[0] == closed


def test_request_helper_enforces_one_shared_budget_across_calls() -> None:
    budget = RetryBudget(RetryProfile("focus", 3, 45, 30))
    token = install_retry_budget(budget)
    request = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.headers = {}
    response.url = "https://api.bcb.gov.br/test"
    response.content = b"[]"
    request.return_value = response
    try:
        for _ in range(3):
            get_provider_response(
                "https://api.bcb.gov.br/test", request=request, timeout=30, max_bytes=10
            )
        with pytest.raises(RetryBudgetExceeded):
            get_provider_response(
                "https://api.bcb.gov.br/test", request=request, timeout=30, max_bytes=10
            )
    finally:
        reset_retry_budget(token)
    assert request.call_count == 3
    assert budget.requests == 3


@pytest.fixture
def isolated_sqlite_policy_db(tmp_path, monkeypatch):
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DATABASE_URL", "")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "policy.db")
    monkeypatch.setattr(store, "_pg_local", local())
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def test_breaker_opens_after_three_failures_and_allows_one_half_open_probe(
    isolated_sqlite_policy_db,
) -> None:
    scope = "bcb_sgs:primary:primary"
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    for count in range(1, 4):
        claim_time = now + timedelta(seconds=count)
        admitted, _, _, _ = store.claim_source_access_probe(
            scope, claim_time.isoformat(), (claim_time + timedelta(seconds=30)).isoformat()
        )
        assert admitted
        store.record_source_access_failure(
            scope,
            claim_time.isoformat(),
            (claim_time + timedelta(minutes=5)).isoformat(),
            None,
            rate_limited=False,
        )
    denied, _, _, _ = store.claim_source_access_probe(
        scope, (now + timedelta(minutes=1)).isoformat(), (now + timedelta(minutes=2)).isoformat()
    )
    assert not denied
    probe_at = now + timedelta(minutes=6)
    admitted, _, half_open, _ = store.claim_source_access_probe(
        scope, probe_at.isoformat(), (probe_at + timedelta(seconds=30)).isoformat()
    )
    assert admitted
    assert half_open
    concurrent, _, _, _ = store.claim_source_access_probe(
        scope, probe_at.isoformat(), (probe_at + timedelta(seconds=30)).isoformat()
    )
    assert not concurrent
    expired_probe = probe_at + timedelta(seconds=31)
    reclaimed, _, half_open, generation = store.claim_source_access_probe(
        scope, expired_probe.isoformat(), (expired_probe + timedelta(seconds=30)).isoformat()
    )
    assert reclaimed and half_open
    store.record_source_access_success(scope, probe_at.isoformat(), generation)
    normal, _, half_open, _ = store.claim_source_access_probe(
        scope, probe_at.isoformat(), (probe_at + timedelta(seconds=30)).isoformat()
    )
    assert normal
    assert not half_open
    row = store._query("SELECT * FROM source_access_state WHERE scope_id = ?", (scope,))[0]
    assert (row["state"], row["consecutive_temporary_failures"]) == ("closed", 0)


def test_rate_limit_opens_breaker_immediately_and_honors_later_retry_time(
    isolated_sqlite_policy_db,
) -> None:
    scope = "anbima:primary:primary"
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    retry_at = now + timedelta(hours=1)
    store.claim_source_access_probe(scope, now.isoformat(), (now + timedelta(minutes=3)).isoformat())
    store.record_source_access_failure(
        scope, now.isoformat(), (now + timedelta(minutes=5)).isoformat(),
        retry_at.isoformat(), rate_limited=True,
    )
    row = store._query("SELECT consecutive_temporary_failures FROM source_access_state WHERE scope_id = ?", (scope,))[0]
    assert row["consecutive_temporary_failures"] == 0
    early, not_before, _, _ = store.claim_source_access_probe(
        scope, (now + timedelta(minutes=20)).isoformat(), (now + timedelta(minutes=23)).isoformat()
    )
    assert not early
    assert not_before == retry_at.isoformat()
    due, _, half_open, _ = store.claim_source_access_probe(
        scope, retry_at.isoformat(), (retry_at + timedelta(minutes=3)).isoformat()
    )
    assert due
    assert half_open


def test_fresh_focus_cache_hit_does_not_touch_an_open_breaker(
    isolated_sqlite_policy_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datafetchers import focus

    scope = "focus:primary:primary"
    now = datetime.now(timezone.utc)
    opened_until = (now + timedelta(minutes=5)).isoformat()
    for _ in range(3):
        store.record_source_access_failure(
            scope, now.isoformat(), opened_until, None, rate_limited=False
        )
    before = store._query(
        "SELECT * FROM source_access_state WHERE scope_id = ?", (scope,)
    )[0]
    cached = [{
        "Data": now.date().isoformat(), "DataReferencia": now.year,
        "Indicador": "IPCA", "baseCalculo": 0, "Suavizada": "S", "Mediana": 4.2,
    }]
    monkeypatch.setattr(focus, "get_meta", lambda _key: now.isoformat())
    monkeypatch.setattr(focus, "query_focus", lambda _indicator: {"IPCA": cached})
    network = MagicMock(side_effect=AssertionError("cache hit must not make HTTP"))
    monkeypatch.setattr(focus, "get_provider_response", network)

    assert focus.fetch_focus("IPCA", use_cache=True) == cached

    after = store._query(
        "SELECT * FROM source_access_state WHERE scope_id = ?", (scope,)
    )[0]
    assert after == before
    network.assert_not_called()
