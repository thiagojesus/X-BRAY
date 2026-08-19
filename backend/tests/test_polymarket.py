import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SAMPLE_EVENT = {
    "markets": [
        {
            "id": 1,
            "question": "Will Flavio Bolsonaro win the most votes in the next Brazil presidential election from São Paulo?",
            "groupItemTitle": "Flavio Bolsonaro",
            "lastTradePrice": 0.89,
            "volume": 3000.6,
            "clobTokenIds": '["111", "222"]',
        },
        {
            "id": 2,
            "question": "Will Lula win the most votes in the next Brazil presidential election from São Paulo?",
            "groupItemTitle": "Lula",
            "lastTradePrice": 0.08,
            "volume": 1027.2,
            "clobTokenIds": '["333", "444"]',
        },
        {
            "id": 3,
            "question": "Will Zema win the most votes in the next Brazil presidential election from São Paulo?",
            "groupItemTitle": "Zema",
            "lastTradePrice": 0.13,
            "volume": 403.9,
            "clobTokenIds": '["555", "666"]',
        },
        {
            "id": 4,
            "question": "Will Candidate A win the most votes in the next Brazil presidential election from São Paulo?",
            "groupItemTitle": "Candidate A",
            "lastTradePrice": 0,
            "volume": 0,
            "clobTokenIds": '["777", "888"]',
        },
        {
            "id": 5,
            "question": "Will Person N win the most votes in the next Brazil presidential election from São Paulo?",
            "groupItemTitle": "Person N",
            "lastTradePrice": 0.5,
            "volume": 500,
            "clobTokenIds": '["999", "000"]',
        },
        {
            "id": 6,
            "question": "Will Low Volume Guy win the most votes in the next Brazil presidential election from São Paulo?",
            "groupItemTitle": "Low Volume Guy",
            "lastTradePrice": 0.001,
            "volume": 10,
            "clobTokenIds": '["aaa", "bbb"]',
        },
    ]
}

SAMPLE_HISTORY = {
    "history": [
        {"t": 1787011231, "p": 0.84},
        {"t": 1787172310, "p": 0.885},
    ]
}


@pytest.fixture(autouse=True)
def _reset_db():
    import db.store as store_mod
    db_path = Path(__file__).resolve().parent.parent / "test_xbry.db"
    if db_path.exists():
        db_path.unlink()
    store_mod._conn = None
    store_mod.DB_PATH = db_path
    store_mod.init_db()
    yield
    store_mod._conn = None
    if db_path.exists():
        db_path.unlink()


class TestCandidateFilter:
    def test_excludes_placeholders(self):
        from datafetchers.polymarket import _is_valid_candidate
        assert _is_valid_candidate("Flavio Bolsonaro") is True
        assert _is_valid_candidate("Lula") is True
        assert _is_valid_candidate("Candidate A") is False
        assert _is_valid_candidate("Person N") is False
        assert _is_valid_candidate("another person") is False
        assert _is_valid_candidate("") is False

    def test_date_key(self):
        from datafetchers.polymarket import _to_date_key
        assert _to_date_key(1787011231) == "2026-08-18"
        assert _to_date_key(1787172310) == "2026-08-19"


class TestFetchState:
    def test_fetch_state_filters_and_aggregates(self):
        from datafetchers.polymarket import _fetch_state

        def fake_get(url, timeout=30):
            resp = MagicMock()
            if "events" in url:
                resp.json.return_value = [SAMPLE_EVENT]
            else:
                resp.json.return_value = SAMPLE_HISTORY
            resp.raise_for_status = MagicMock()
            return resp

        with patch("datafetchers.polymarket.httpx.get", side_effect=fake_get):
            state = _fetch_state("SP")

        names = [c["name"] for c in state["candidates"]]
        assert names == ["Flavio Bolsonaro", "Lula", "Zema"]
        assert "Candidate A" not in names
        assert "Person N" not in names
        assert "Low Volume Guy" not in names

        assert "2026-08-18" in state["history"]
        assert "2026-08-19" in state["history"]
        assert state["history"]["2026-08-19"]["Flavio Bolsonaro"] == 88.5

    def test_fetch_state_top4_limit(self):
        from datafetchers.polymarket import _fetch_state

        markets = []
        for i in range(8):
            markets.append({
                "groupItemTitle": f"Cand{i}",
                "lastTradePrice": 0.1,
                "volume": 1000 + i,
                "clobTokenIds": json.dumps([str(1000 + i)]),
            })
        event = {"markets": markets}

        def fake_get(url, timeout=30):
            resp = MagicMock()
            if "events" in url:
                resp.json.return_value = [event]
            else:
                resp.json.return_value = {"history": []}
            resp.raise_for_status = MagicMock()
            return resp

        with patch("datafetchers.polymarket.httpx.get", side_effect=fake_get):
            state = _fetch_state("SP")

        assert len(state["candidates"]) <= 4


class TestFetchStatePolls:
    def test_full_payload_shape(self):
        from datafetchers.polymarket import fetch_state_polls

        def fake_get(url, timeout=30):
            resp = MagicMock()
            if "events" in url:
                resp.json.return_value = [SAMPLE_EVENT]
            else:
                resp.json.return_value = SAMPLE_HISTORY
            resp.raise_for_status = MagicMock()
            return resp

        with patch("datafetchers.polymarket.httpx.get", side_effect=fake_get):
            payload = fetch_state_polls(use_cache=False)

        assert payload["source"] == "Polymarket"
        assert len(payload["ufs"]) == 27
        assert payload["days"] == ["2026-08-18", "2026-08-19"]
        assert all(s["uf"] for s in payload["ufs"])
        assert all(s["history"] for s in payload["ufs"])

    def test_cache_used_on_second_call(self):
        from datafetchers.polymarket import fetch_state_polls, CACHE_KEY
        import db.store as store_mod

        def fake_get(url, timeout=30):
            resp = MagicMock()
            if "events" in url:
                resp.json.return_value = [SAMPLE_EVENT]
            else:
                resp.json.return_value = SAMPLE_HISTORY
            resp.raise_for_status = MagicMock()
            return resp

        with patch("datafetchers.polymarket.httpx.get", side_effect=fake_get) as mock_get:
            payload1 = fetch_state_polls(use_cache=False)
            first_calls = mock_get.call_count
            payload2 = fetch_state_polls(use_cache=True)
            assert mock_get.call_count == first_calls
            assert payload1 == payload2

    def test_cache_expiry_forces_refetch(self):
        from datafetchers.polymarket import fetch_state_polls, CACHE_KEY
        import db.store as store_mod

        def fake_get(url, timeout=30):
            resp = MagicMock()
            if "events" in url:
                resp.json.return_value = [SAMPLE_EVENT]
            else:
                resp.json.return_value = SAMPLE_HISTORY
            resp.raise_for_status = MagicMock()
            return resp

        with patch("datafetchers.polymarket.httpx.get", side_effect=fake_get) as mock_get:
            fetch_state_polls(use_cache=False)
            store_mod.set_meta(f"{CACHE_KEY}_ts", "2020-01-01T00:00:00")
            before = mock_get.call_count
            fetch_state_polls(use_cache=True)
            assert mock_get.call_count > before


class TestRoute:
    def test_get_states_endpoint(self):
        from fastapi.testclient import TestClient

        def fake_get(url, timeout=30):
            resp = MagicMock()
            if "events" in url:
                resp.json.return_value = [SAMPLE_EVENT]
            else:
                resp.json.return_value = SAMPLE_HISTORY
            resp.raise_for_status = MagicMock()
            return resp

        with patch("datafetchers.polymarket.httpx.get", side_effect=fake_get):
            from main import app
            c = TestClient(app)
            r = c.get("/api/eleicoes/estados")
            assert r.status_code == 200
            data = r.json()
            assert data["source"] == "Polymarket"
            assert len(data["ufs"]) == 27

    def test_refresh_endpoint(self):
        from fastapi.testclient import TestClient

        def fake_get(url, timeout=30):
            resp = MagicMock()
            if "events" in url:
                resp.json.return_value = [SAMPLE_EVENT]
            else:
                resp.json.return_value = SAMPLE_HISTORY
            resp.raise_for_status = MagicMock()
            return resp

        with patch("datafetchers.polymarket.httpx.get", side_effect=fake_get):
            from main import app
            c = TestClient(app)
            r = c.post("/api/eleicoes/estados/refresh")
            assert r.status_code == 200
            assert r.json()["status"] == "refreshed"