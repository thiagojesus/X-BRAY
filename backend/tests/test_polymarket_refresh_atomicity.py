from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from datafetchers.polymarket import (
    PolymarketRefreshError,
    StatePoll,
    StatePollPayload,
    _cache_set,
    fetch_state_polls,
    read_state_polls,
)


@pytest.fixture(autouse=True)
def _reset_db() -> Iterator[None]:
    import db.store as store_mod

    db_path = Path(__file__).resolve().parent.parent / "test_xbry.db"
    if store_mod._conn is not None:
        store_mod._conn.close()
    if db_path.exists():
        db_path.unlink()
    store_mod._conn = None
    store_mod.DB_PATH = db_path
    store_mod.init_db()
    yield
    if store_mod._conn is not None:
        store_mod._conn.close()
    store_mod._conn = None
    if db_path.exists():
        db_path.unlink()


class StateProviderError(RuntimeError):
    uf: str

    def __init__(self, uf: str) -> None:
        self.uf = uf
        super().__init__(f"{uf} provider failed")


def test_failed_partial_refresh_preserves_previous_snapshot() -> None:
    previous: StatePollPayload = {
        "source": "Polymarket",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "national": {
            "candidates": [{"name": "Lula", "price": 0.5, "volume": 1000}],
            "history": {"2026-08-01": {"Lula": 50.0}},
        },
        "days": ["2026-08-01"],
        "ufs": [{"uf": "SP", "candidates": [], "history": {}}],
    }
    _cache_set(previous)

    def fetch_state(uf: str) -> StatePoll:
        if uf == "SP":
            raise StateProviderError(uf)
        return {"uf": uf, "candidates": [], "history": {}}

    with (
        patch("datafetchers.polymarket._fetch_national", return_value=previous["national"]),
        patch("datafetchers.polymarket._fetch_state", side_effect=fetch_state),
        pytest.raises(PolymarketRefreshError, match="SP"),
    ):
        fetch_state_polls(use_cache=False)

    assert read_state_polls() == previous
