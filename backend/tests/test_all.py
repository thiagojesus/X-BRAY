import sqlite3
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import date, datetime

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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


@pytest.fixture
def _sample_sgs():
    return [
        {"data": "01/01/2025", "valor": "10,5"},
        {"data": "01/02/2025", "valor": "11,2"},
        {"data": "01/03/2025", "valor": "12,0"},
    ]


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from main import app
    with patch("datafetchers.bcb_sgs.httpx.get", side_effect=Exception("mocked")), \
         patch("datafetchers.anbima.httpx.get", side_effect=Exception("mocked")), \
         patch("datafetchers.focus.httpx.get", side_effect=Exception("mocked")), \
         patch("datafetchers.b3_di.httpx.get", side_effect=Exception("mocked")), \
         patch("main._refresh_background"), \
         TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestConfig:
    def test_bcb_sgs_base_url(self):
        from config import BCB_SGS_BASE
        assert "bcb.gov.br" in BCB_SGS_BASE
        assert "{code}" in BCB_SGS_BASE

    def test_bcb_odata_base_url(self):
        from config import BCB_ODATA_BASE
        assert "olinda.bcb.gov.br" in BCB_ODATA_BASE

    def test_anbima_url(self):
        from config import ANBIMA_XLS_URL
        assert "s3" in ANBIMA_XLS_URL.lower() or "anbima" in ANBIMA_XLS_URL.lower()

    def test_inflation_codes(self):
        from config import INFLATION
        assert "ipca" in INFLATION
        assert INFLATION["ipca"] == 433
        assert "igpdi" in INFLATION
        assert "incc_di" in INFLATION

    def test_interest_rates_codes(self):
        from config import INTEREST_RATES
        assert INTEREST_RATES["selic_meta"] == 432
        assert INTEREST_RATES["cdi"] == 12
        assert INTEREST_RATES["tr"] == 226
        assert INTEREST_RATES["selic_efetiva"] == 11

    def test_activity_codes(self):
        from config import ACTIVITY
        assert "pib" in ACTIVITY
        assert "ibc_br" in ACTIVITY
        assert "desemprego" in ACTIVITY
        assert "resultado_primario" in ACTIVITY

    def test_exchange_codes(self):
        from config import EXCHANGE
        assert "ptax_compra_usd" in EXCHANGE
        assert "ptax_venda_usd" in EXCHANGE
        assert "eur_brl" in EXCHANGE

    def test_complementary_codes(self):
        from config import COMPLEMENTARY
        assert "reservas_internacionais" in COMPLEMENTARY
        assert "base_monetaria" in COMPLEMENTARY
        assert "ic_commodities" in COMPLEMENTARY

    def test_ipca_groups(self):
        from config import IPCA_GROUPS
        assert len(IPCA_GROUPS) == 8
        expected = {"alimentacao_bebidas", "habitacao", "artigos_residencia", "vestuario",
                     "transportes", "comunicacao", "saude_cuidados", "despesas_pessoais"}
        assert set(IPCA_GROUPS.keys()) == expected

    def test_ipca_nature(self):
        from config import IPCA_NATURE
        assert len(IPCA_NATURE) == 4
        assert "bens_duraveis" in IPCA_NATURE
        assert "servicos" in IPCA_NATURE

    def test_ipca_core(self):
        from config import IPCA_CORE
        assert "core_ex1" not in IPCA_CORE
        assert "core_medias_aparadas" in IPCA_CORE
        assert "core_dp" in IPCA_CORE

    def test_ipca_prices(self):
        from config import IPCA_PRICES
        assert len(IPCA_PRICES) == 4
        assert "itens_livres" in IPCA_PRICES
        assert "administrados" in IPCA_PRICES


class TestStoreInit:
    def test_init_db_creates_tables(self):
        import db.store as store_mod
        conn = store_mod._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t["name"] for t in tables}
        assert "sgs" in table_names
        assert "anbima" in table_names
        assert "focus" in table_names
        assert "meta" in table_names

    def test_init_db_idempotent(self):
        import db.store as store_mod
        store_mod.init_db()
        store_mod.init_db()


class TestSGSStore:
    def test_upsert_and_query(self, _sample_sgs):
        from db.store import upsert_sgs, query_sgs
        upsert_sgs(433, _sample_sgs)
        result = query_sgs(433)
        assert len(result) == 3
        assert result[0]["data"] == "01/01/2025"
        assert result[0]["valor"] == "10.5"
        assert result[1]["data"] == "01/02/2025"
        assert result[2]["data"] == "01/03/2025"

    def test_upsert_deduplicates(self, _sample_sgs):
        from db.store import upsert_sgs, query_sgs
        upsert_sgs(433, _sample_sgs)
        upsert_sgs(433, _sample_sgs)
        result = query_sgs(433)
        assert len(result) == 3

    def test_upsert_replaces_on_conflict(self):
        from db.store import upsert_sgs, query_sgs
        upsert_sgs(433, [{"data": "01/01/2025", "valor": "10,0"}])
        upsert_sgs(433, [{"data": "01/01/2025", "valor": "20,0"}])
        result = query_sgs(433)
        assert len(result) == 1
        assert result[0]["valor"] == "20.0"

    def test_query_sgs_date_range(self, _sample_sgs):
        from db.store import upsert_sgs, query_sgs
        upsert_sgs(433, _sample_sgs)
        result = query_sgs(433, start_date="2025-02-01", end_date="2025-02-28")
        assert len(result) == 1
        assert result[0]["data"] == "01/02/2025"

    def test_query_sgs_start_only(self, _sample_sgs):
        from db.store import upsert_sgs, query_sgs
        upsert_sgs(433, _sample_sgs)
        result = query_sgs(433, start_date="2025-02-01")
        assert len(result) == 2

    def test_query_sgs_end_only(self, _sample_sgs):
        from db.store import upsert_sgs, query_sgs
        upsert_sgs(433, _sample_sgs)
        result = query_sgs(433, end_date="2025-02-01")
        assert len(result) == 2

    def test_query_sgs_empty(self):
        from db.store import query_sgs
        result = query_sgs(99999)
        assert result == []

    def test_upsert_skips_malformed(self):
        from db.store import upsert_sgs, query_sgs
        upsert_sgs(433, [
            {"data": "01/01/2025", "valor": "10,0"},
            {"data": "bad", "valor": "xx"},
            {"data": "01/02/2025", "valor": "15,0"},
        ])
        result = query_sgs(433)
        assert len(result) == 2

    def test_query_sgs_latest(self, _sample_sgs):
        from db.store import upsert_sgs, query_sgs_latest
        upsert_sgs(433, _sample_sgs)
        result = query_sgs_latest(433, n=2)
        assert len(result) == 2
        assert result[0]["data"] == "01/02/2025"
        assert result[1]["data"] == "01/03/2025"

    def test_get_sgs_range(self, _sample_sgs):
        from db.store import upsert_sgs, get_sgs_range
        upsert_sgs(433, _sample_sgs)
        min_d, max_d = get_sgs_range(433)
        assert min_d == "2025-01-01"
        assert max_d == "2025-03-01"

    def test_get_sgs_range_empty(self):
        from db.store import get_sgs_range
        min_d, max_d = get_sgs_range(99999)
        assert min_d is None
        assert max_d is None

    def test_different_series_isolated(self, _sample_sgs):
        from db.store import upsert_sgs, query_sgs
        upsert_sgs(433, _sample_sgs)
        upsert_sgs(434, [{"data": "01/06/2025", "valor": "5,0"}])
        assert len(query_sgs(433)) == 3
        assert len(query_sgs(434)) == 1


class TestAnbimaStore:
    def test_upsert_and_query(self):
        from db.store import upsert_anbima, query_anbima
        records = [
            {"Data de Referência": "2025-01-01", "valor": 100},
            {"Data de Referência": "2025-02-01", "valor": 200},
        ]
        upsert_anbima("ima", records)
        result = query_anbima("ima")
        assert "ima" in result
        assert len(result["ima"]) == 2
        assert result["ima"][0]["valor"] == 100

    def test_query_all_sheets(self):
        from db.store import upsert_anbima, query_anbima
        upsert_anbima("sheet1", [{"a": 1}])
        upsert_anbima("sheet2", [{"b": 2}])
        result = query_anbima()
        assert len(result) == 2
        assert "sheet1" in result
        assert "sheet2" in result

    def test_upsert_replaces_same_index(self):
        from db.store import upsert_anbima, query_anbima
        upsert_anbima("test", [{"val": 1}])
        upsert_anbima("test", [{"val": 2}])
        result = query_anbima("test")
        assert len(result["test"]) == 1
        assert result["test"][0]["val"] == 2

    def test_upsert_different_indices(self):
        from db.store import upsert_anbima, query_anbima
        upsert_anbima("test", [{"val": 1}])
        upsert_anbima("test", [{"val": 2}])
        result = query_anbima("test")
        assert len(result["test"]) >= 1


class TestFocusStore:
    def test_upsert_and_query(self):
        from db.store import upsert_focus, query_focus
        records = [
            {"Data": "2025-01-10T00:00:00", "valor": 4.5},
            {"Data": "2025-02-10T00:00:00", "valor": 4.25},
        ]
        upsert_focus("IPCA", records)
        result = query_focus("IPCA")
        assert "IPCA" in result
        assert len(result["IPCA"]) == 2

    def test_query_all_indicators(self):
        from db.store import upsert_focus, query_focus
        upsert_focus("IPCA", [{"Data": "2025-01-10", "v": 1}])
        upsert_focus("Selic", [{"Data": "2025-01-10", "v": 2}])
        result = query_focus()
        assert len(result) == 2

    def test_upsert_skips_no_date(self):
        from db.store import upsert_focus, query_focus
        upsert_focus("IPCA", [{"Data": ""}, {"Data": None}])
        result = query_focus("IPCA")
        assert len(result.get("IPCA", [])) == 0


class TestMetaStore:
    def test_set_and_get(self):
        from db.store import set_meta, get_meta
        set_meta("test_key", "test_value")
        assert get_meta("test_key") == "test_value"

    def test_get_missing(self):
        from db.store import get_meta
        assert get_meta("nonexistent") is None

    def test_overwrite(self):
        from db.store import set_meta, get_meta
        set_meta("k", "v1")
        set_meta("k", "v2")
        assert get_meta("k") == "v2"


class TestDBStats:
    def test_db_stats(self):
        from db.store import db_stats
        stats = db_stats()
        assert "sgs" in stats
        assert "anbima" in stats
        assert "focus" in stats
        assert all(isinstance(v, int) for v in stats.values())


class TestDateParsing:
    def test_parse_sgs_date(self):
        from db.store import _parse_sgs_date
        assert _parse_sgs_date("15/03/2025") == "2025-03-15"
        assert _parse_sgs_date("01/01/2020") == "2020-01-01"

    def test_to_sgs_date(self):
        from db.store import _to_sgs_date
        assert _to_sgs_date("2025-03-15") == "15/03/2025"

    def test_bcb_parse_date(self):
        from datafetchers.bcb_sgs import _parse_date
        d = _parse_date("15/03/2025")
        assert d == date(2025, 3, 15)

    def test_bcb_format_date(self):
        from datafetchers.bcb_sgs import _format_date
        assert _format_date(date(2025, 3, 15)) == "15/03/2025"


class TestMakeWindows:
    def test_no_start(self):
        from datafetchers.bcb_sgs import _make_windows
        result = _make_windows(None, "15/03/2025")
        assert result == [(None, "15/03/2025")]

    def test_single_window(self):
        from datafetchers.bcb_sgs import _make_windows
        result = _make_windows("01/01/2020", "15/03/2025")
        assert len(result) == 1
        assert result[0] == ("01/01/2020", "15/03/2025")

    def test_multi_window(self):
        from datafetchers.bcb_sgs import _make_windows
        result = _make_windows("01/01/2015", "01/01/2026")
        assert len(result) == 2
        assert result[0][0] == "01/01/2015"
        assert result[0][1] == "01/01/2025"
        assert result[1][0] == "01/01/2025"

    def test_cursor_no_gap(self):
        from datafetchers.bcb_sgs import _make_windows
        result = _make_windows("01/01/2020", "01/01/2021")
        assert len(result) == 1


class TestBCGSNeedsRefresh:
    def test_needs_refresh_no_meta(self):
        from datafetchers.bcb_sgs import _needs_refresh
        assert _needs_refresh(433) is True

    def test_needs_refresh_recent(self):
        from db.store import set_meta
        from datafetchers.bcb_sgs import _needs_refresh
        set_meta("sgs_last_refresh_433", datetime.now().isoformat())
        assert _needs_refresh(433) is False

    def test_needs_refresh_old(self):
        from db.store import set_meta
        from datafetchers.bcb_sgs import _needs_refresh
        from datetime import timedelta
        old = datetime.now() - timedelta(hours=24)
        set_meta("sgs_last_refresh_433", old.isoformat())
        assert _needs_refresh(433) is True

    def test_needs_refresh_invalid(self):
        from db.store import set_meta
        from datafetchers.bcb_sgs import _needs_refresh
        set_meta("sgs_last_refresh_433", "not-a-date")
        assert _needs_refresh(433) is True


class TestFetchSGSSeries:
    @patch("datafetchers.bcb_sgs.httpx.get")
    def test_fetch_from_api(self, mock_get):
        from datafetchers.bcb_sgs import fetch_sgs_series
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"data": "01/01/2025", "valor": "10,5"},
            {"data": "01/02/2025", "valor": "11,2"},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp
        result = fetch_sgs_series(433, start_date="01/01/2025", end_date="28/02/2025", use_cache=False)
        assert len(result) == 2
        mock_get.assert_called()

    @patch("datafetchers.bcb_sgs.httpx.get")
    def test_fetch_api_error_returns_cached(self, mock_get):
        from datafetchers.bcb_sgs import fetch_sgs_series
        from db.store import upsert_sgs
        upsert_sgs(433, [{"data": "01/01/2025", "valor": "5,0"}])
        mock_get.side_effect = Exception("timeout")
        result = fetch_sgs_series(433, start_date="01/01/2025", end_date="28/02/2025", use_cache=False)
        assert len(result) == 1

    def test_fetch_known_unavailable(self):
        from datafetchers.bcb_sgs import fetch_sgs_series, KNOWN_UNAVAILABLE
        from db.store import upsert_sgs
        original = set(KNOWN_UNAVAILABLE)
        KNOWN_UNAVAILABLE.add(99999)
        try:
            upsert_sgs(99999, [{"data": "01/01/2025", "valor": "1,0"}])
            result = fetch_sgs_series(99999)
            assert len(result) == 1
        finally:
            KNOWN_UNAVAILABLE.clear()
            KNOWN_UNAVAILABLE.update(original)

    @patch("datafetchers.bcb_sgs.httpx.get")
    def test_fetch_uses_cache_when_fresh(self, mock_get):
        from datafetchers.bcb_sgs import fetch_sgs_series
        from db.store import upsert_sgs, set_meta
        upsert_sgs(433, [{"data": "01/01/2025", "valor": "5,0"}])
        set_meta("sgs_last_refresh_433", datetime.now().isoformat())
        result = fetch_sgs_series(433, use_cache=True)
        mock_get.assert_not_called()
        assert len(result) == 1

    @patch("datafetchers.bcb_sgs.httpx.get")
    def test_fetch_empty_api_returns_empty(self, mock_get):
        from datafetchers.bcb_sgs import fetch_sgs_series
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp
        result = fetch_sgs_series(433, start_date="01/01/2025", end_date="28/02/2025", use_cache=False)
        assert result == []


class TestFetchSGSBatch:
    @patch("datafetchers.bcb_sgs.fetch_sgs_series")
    def test_batch_multiple(self, mock_fetch):
        from datafetchers.bcb_sgs import fetch_sgs_batch
        mock_fetch.return_value = [{"data": "01/01/2025", "valor": "1,0"}]
        result = fetch_sgs_batch({"a": 1, "b": 2}, start_date="01/01/2025")
        assert "a" in result
        assert "b" in result
        assert mock_fetch.call_count == 2

    @patch("datafetchers.bcb_sgs.fetch_sgs_series")
    def test_batch_handles_exception(self, mock_fetch):
        from datafetchers.bcb_sgs import fetch_sgs_batch
        mock_fetch.side_effect = Exception("fail")
        result = fetch_sgs_batch({"a": 1})
        assert "error" in result["a"]


class TestForceRefreshSGS:
    @patch("datafetchers.bcb_sgs.httpx.get")
    def test_force_refresh(self, mock_get):
        from datafetchers.bcb_sgs import force_refresh_sgs
        from db.store import get_meta
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"data": "01/01/2025", "valor": "10,0"}]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp
        result = force_refresh_sgs(433)
        assert len(result) >= 1
        assert get_meta("sgs_last_refresh_433") is not None


class TestFetchSGSLastN:
    @patch("datafetchers.bcb_sgs.httpx.get")
    def test_fetch_last_n(self, mock_get):
        from datafetchers.bcb_sgs import fetch_sgs_last_n
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"data": "01/06/2025", "valor": "10,0"},
            {"data": "01/07/2025", "valor": "11,0"},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp
        result = fetch_sgs_last_n(433, n=2)
        assert len(result) == 2


class TestAnbimaFetcher:
    def test_needs_refresh_no_meta(self):
        from datafetchers.anbima import _needs_refresh
        assert _needs_refresh() is True

    def test_needs_refresh_recent(self):
        from db.store import set_meta
        from datafetchers.anbima import _needs_refresh
        set_meta("anbima_last_refresh", datetime.now().isoformat())
        assert _needs_refresh() is False

    @patch("datafetchers.anbima.httpx.get")
    def test_fetch_error_returns_dict(self, mock_get):
        from datafetchers.anbima import fetch_anbima_ima
        mock_get.side_effect = Exception("network")
        result = fetch_anbima_ima(use_cache=False)
        assert "error" in result


class TestFocusFetcher:
    def test_needs_refresh_no_meta(self):
        from datafetchers.focus import _needs_refresh
        assert _needs_refresh() is True

    def test_needs_refresh_recent(self):
        from db.store import set_meta
        from datafetchers.focus import _needs_refresh
        set_meta("focus_last_refresh", datetime.now().isoformat())
        assert _needs_refresh() is False

    @patch("datafetchers.focus.httpx.get")
    def test_fetch_focus_api(self, mock_get):
        from datafetchers.focus import fetch_focus
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"value": [{"Data": "2025-01-10", "v": 1}]}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp
        result = fetch_focus("IPCA", use_cache=False)
        assert len(result) >= 1

    @patch("datafetchers.focus.httpx.get")
    def test_fetch_focus_api_error(self, mock_get):
        from datafetchers.focus import fetch_focus
        mock_get.side_effect = Exception("fail")
        result = fetch_focus("IPCA", use_cache=False)
        assert isinstance(result, list)

    @patch("datafetchers.focus.fetch_focus")
    def test_fetch_all_focus(self, mock_fetch):
        from datafetchers.focus import fetch_all_focus
        mock_fetch.return_value = [{"Data": "2025-01-10"}]
        result = fetch_all_focus()
        assert len(result) == 5
        assert "IPCA" in result
        assert "Selic" in result

    def test_force_refresh_focus(self):
        from datafetchers.focus import force_refresh_focus
        with patch("datafetchers.focus.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"value": []}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = force_refresh_focus()
            assert isinstance(result, dict)


class TestFastAPIRoutes:
    def test_root(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "X-BRAY API"
        assert data["version"] == "2.0.0"
        assert "endpoints" in data

    def test_status(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert "db_stats" in data
        assert "timestamp" in data

    def test_refresh(self, client):
        resp = client.post("/api/refresh")
        assert resp.status_code == 200
        assert "refresh completed" in resp.json()["status"]

    def test_juros(self, client):
        resp = client.get("/api/juros")
        assert resp.status_code == 200
        data = resp.json()
        assert "source" in data
        assert "data" in data
        assert isinstance(data["data"], dict)

    def test_juros_selic_meta(self, client):
        resp = client.get("/api/juros/selic-meta")
        assert resp.status_code == 200
        data = resp.json()
        assert "series" in data
        assert "data" in data

    def test_juros_cdi(self, client):
        resp = client.get("/api/juros/cdi")
        assert resp.status_code == 200
        assert resp.json()["series"] == "CDI"

    def test_juros_tr(self, client):
        resp = client.get("/api/juros/tr")
        assert resp.status_code == 200
        assert resp.json()["series"] == "TR"

    def test_juros_selic_efetiva(self, client):
        resp = client.get("/api/juros/selic-efetiva")
        assert resp.status_code == 200
        assert resp.json()["series"] == "Selic Efetiva"

    def test_juros_refresh(self, client):
        resp = client.post("/api/juros/refresh")
        assert resp.status_code == 200
        assert resp.json()["status"] == "refreshed"

    def test_inflacao(self, client):
        resp = client.get("/api/inflacao")
        assert resp.status_code == 200
        assert "data" in resp.json()

    def test_inflacao_ipca(self, client):
        resp = client.get("/api/inflacao/ipca")
        assert resp.status_code == 200
        assert resp.json()["code"] == 433

    def test_inflacao_ipca_12m(self, client):
        resp = client.get("/api/inflacao/ipca-12m")
        assert resp.status_code == 200
        assert resp.json()["code"] == 13522

    def test_inflacao_igpm(self, client):
        resp = client.get("/api/inflacao/igpm")
        assert resp.status_code == 200
        assert resp.json()["code"] == 189

    def test_inflacao_refresh(self, client):
        resp = client.post("/api/inflacao/refresh")
        assert resp.status_code == 200

    def test_ipca_decomposicao_grupos(self, client):
        resp = client.get("/api/ipca-decomposicao/grupos")
        assert resp.status_code == 200
        assert resp.json()["type"] == "grupos_despesa"

    def test_ipca_decomposicao_naturezas(self, client):
        resp = client.get("/api/ipca-decomposicao/naturezas")
        assert resp.status_code == 200
        assert resp.json()["type"] == "naturezas"

    def test_ipca_decomposicao_core(self, client):
        resp = client.get("/api/ipca-decomposicao/core")
        assert resp.status_code == 200
        assert resp.json()["type"] == "core"

    def test_ipca_decomposicao_precos(self, client):
        resp = client.get("/api/ipca-decomposicao/precos")
        assert resp.status_code == 200
        assert resp.json()["type"] == "livres_administrados"

    def test_ipca_decomposicao_tudo(self, client):
        resp = client.get("/api/ipca-decomposicao/tudo")
        assert resp.status_code == 200
        data = resp.json()
        assert "grupos" in data
        assert "naturezas" in data
        assert "core" in data
        assert "precos" in data

    def test_ipca_decomposicao_refresh(self, client):
        resp = client.post("/api/ipca-decomposicao/refresh")
        assert resp.status_code == 200

    def test_atividade(self, client):
        resp = client.get("/api/atividade")
        assert resp.status_code == 200
        assert "data" in resp.json()

    def test_atividade_pib(self, client):
        resp = client.get("/api/atividade/pib")
        assert resp.status_code == 200
        assert resp.json()["code"] == 1207

    def test_atividade_ibc_br(self, client):
        resp = client.get("/api/atividade/ibc-br")
        assert resp.status_code == 200
        assert resp.json()["code"] == 24363

    def test_atividade_desemprego(self, client):
        resp = client.get("/api/atividade/desemprego")
        assert resp.status_code == 200
        assert resp.json()["code"] == 24369

    def test_atividade_refresh(self, client):
        resp = client.post("/api/atividade/refresh")
        assert resp.status_code == 200

    def test_cambio(self, client):
        resp = client.get("/api/cambio")
        assert resp.status_code == 200
        assert "data" in resp.json()

    def test_cambio_usd(self, client):
        resp = client.get("/api/cambio/usd")
        assert resp.status_code == 200
        data = resp.json()
        assert "compra" in data
        assert "venda" in data

    def test_cambio_eur(self, client):
        resp = client.get("/api/cambio/eur")
        assert resp.status_code == 200
        assert resp.json()["code"] == 21619

    def test_cambio_refresh(self, client):
        resp = client.post("/api/cambio/refresh")
        assert resp.status_code == 200

    def test_titulos(self, client):
        resp = client.get("/api/titulos")
        assert resp.status_code == 200
        assert "data" in resp.json()

    def test_titulos_ima(self, client):
        resp = client.get("/api/titulos/ima")
        assert resp.status_code == 200

    def test_focus(self, client):
        resp = client.get("/api/focus")
        assert resp.status_code == 200
        assert "data" in resp.json()

    def test_focus_indicator(self, client):
        resp = client.get("/api/focus/IPCA")
        assert resp.status_code == 200
        assert resp.json()["indicator"] == "IPCA"

    def test_focus_refresh(self, client):
        resp = client.post("/api/focus/refresh")
        assert resp.status_code == 200

    def test_complementares(self, client):
        resp = client.get("/api/complementares")
        assert resp.status_code == 200
        assert "data" in resp.json()

    def test_complementares_reservas(self, client):
        resp = client.get("/api/complementares/reservas")
        assert resp.status_code == 200

    def test_complementares_base_monetaria(self, client):
        resp = client.get("/api/complementares/base-monetaria")
        assert resp.status_code == 200

    def test_complementares_ic_commodities(self, client):
        resp = client.get("/api/complementares/ic-commodities")
        assert resp.status_code == 200

    def test_complementares_refresh(self, client):
        resp = client.post("/api/complementares/refresh")
        assert resp.status_code == 200


class TestCurvasDi:
    def test_curvas_di_route_empty_db(self, client):
        resp = client.get("/api/curvas-di?days=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "B3 Price Report (SPR)"
        assert isinstance(data["dates"], list)
        assert isinstance(data["curves"], dict)

    def test_curvas_di_route_returns_stored_curves(self):
        from fastapi.testclient import TestClient
        from main import app
        from db.store import upsert_b3_di
        upsert_b3_di([
            {"trade_date": "2026-08-13", "symbol": "DI1U26", "maturity": "2026-09-01", "rate": 13.902},
            {"trade_date": "2026-08-13", "symbol": "DI1V26", "maturity": "2026-10-01", "rate": 13.843},
            {"trade_date": "2026-08-14", "symbol": "DI1U26", "maturity": "2026-09-01", "rate": 13.904},
        ])
        with patch("datafetchers.b3_di.httpx.get", side_effect=Exception("mocked")), \
             patch("main._refresh_background"), \
             TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/api/curvas-di?days=5")
        assert resp.status_code == 200
        data = resp.json()
        assert "13/08/2026" in data["curves"]
        curve = {p["symbol"]: p for p in data["curves"]["13/08/2026"]}
        assert curve["DI1U26"]["rate"] == 13.902
        assert curve["DI1U26"]["maturity"] == "2026-09-01"

    def test_curvas_di_refresh(self, client):
        resp = client.post("/api/curvas-di/refresh?days=3")
        assert resp.status_code == 200
        assert resp.json()["status"] == "refreshed"


class TestB3DiFetcher:
    def test_decode_maturity(self):
        from datafetchers.b3_di import decode_maturity
        assert decode_maturity("DI1F25") == "2025-01-02"
        assert decode_maturity("DI1U26") == "2026-09-01"
        assert decode_maturity("DI1V26") == "2026-10-01"
        assert decode_maturity("DI1X26") == "2026-11-03"
        assert decode_maturity("DI1Z26") == "2026-12-01"
        assert decode_maturity("DI1F27") == "2027-01-04"
        assert decode_maturity("DOLV26") is None
        assert decode_maturity("DI1QQ9") is None

    def test_is_business_day(self):
        from datafetchers.b3_di import is_business_day
        from datetime import date
        assert is_business_day(date(2026, 8, 14)) is True
        assert is_business_day(date(2026, 8, 15)) is False
        assert is_business_day(date(2026, 9, 7)) is False
        assert is_business_day(date(2026, 1, 1)) is False

    def test_list_business_days(self):
        from datafetchers.b3_di import list_business_days
        from datetime import date
        days = list_business_days(date(2026, 8, 14), 5)
        assert [d.isoformat() for d in days] == [
            "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"
        ]

    def test_parse_spr_valid_xml(self):
        import io
        import zipfile
        from datafetchers.b3_di import _parse_spr

        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<SecurityList xmlns="urn:bvmf.217.01.xsd">
  <PricRpt>
    <TradDt><Dt>2026-08-14</Dt></TradDt>
    <SctyId><TckrSymb>DI1U26</TckrSymb></SctyId>
    <FinInstrmAttrbts><AdjstdQtTax>13.904</AdjstdQtTax></FinInstrmAttrbts>
  </PricRpt>
  <PricRpt>
    <TradDt><Dt>2026-08-14</Dt></TradDt>
    <SctyId><TckrSymb>DOLV26</TckrSymb></SctyId>
    <FinInstrmAttrbts><AdjstdQtTax>5.5</AdjstdQtTax></FinInstrmAttrbts>
  </PricRpt>
  <PricRpt>
    <TradDt><Dt>2026-08-14</Dt></TradDt>
    <SctyId><TckrSymb>DI1BOGUS</TckrSymb></SctyId>
    <FinInstrmAttrbts><AdjstdQtTax>13.1</AdjstdQtTax></FinInstrmAttrbts>
  </PricRpt>
</SecurityList>
"""
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr("BVBG.187.01_foo.xml", xml)
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as zf:
            zf.writestr("SPRD260814.zip", inner.getvalue())

        records = _parse_spr(outer.getvalue())
        assert len(records) == 1
        assert records[0]["symbol"] == "DI1U26"
        assert records[0]["maturity"] == "2026-09-01"
        assert records[0]["rate"] == 13.904

    def test_parse_spr_empty_zip(self):
        import io
        import zipfile
        from datafetchers.b3_di import _parse_spr
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass
        assert _parse_spr(buf.getvalue()) == []

    def test_parse_spr_bad_zip(self):
        from datafetchers.b3_di import _parse_spr
        assert _parse_spr(b"not a zip") == []


class TestDailyRefresh:
    def test_daily_refresh_runs(self):
        from unittest.mock import patch
        from main import daily_refresh
        with patch("main.fetch_sgs_batch") as mock_batch, \
             patch("main.fetch_all_focus") as mock_focus, \
             patch("main.fetch_anbima_ima") as mock_anbima, \
             patch("main.fetch_di_curves") as mock_di:
            mock_batch.return_value = {}
            mock_focus.return_value = {}
            mock_anbima.return_value = {}
            mock_di.return_value = {}
            daily_refresh()
            assert mock_batch.call_count == 5
            mock_focus.assert_called_once()
            mock_anbima.assert_called_once()
            mock_di.assert_called_once()

    def test_daily_refresh_handles_error(self):
        from unittest.mock import patch
        from main import daily_refresh
        with patch("main.fetch_sgs_batch", side_effect=Exception("fail")), \
             patch("main.fetch_di_curves"):
            daily_refresh()
