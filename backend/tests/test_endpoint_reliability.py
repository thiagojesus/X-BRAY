import json
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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


@pytest.fixture
def client() -> Iterator[TestClient]:
    from main import app

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_query_sgs_batch_returns_each_named_series() -> None:
    # Given
    from db.cached_reads import query_sgs_batch
    from db.store import upsert_sgs

    upsert_sgs(11, [{"data": "01/01/2026", "valor": "14.9"}])
    upsert_sgs(12, [{"data": "01/01/2026", "valor": "14.8"}])

    # When
    result = query_sgs_batch({"selic": 11, "cdi": 12})

    # Then
    assert result == {
        "selic": [{"data": "01/01/2026", "valor": "14.9"}],
        "cdi": [{"data": "01/01/2026", "valor": "14.8"}],
    }


def test_juros_get_reads_database_without_provider_request(client: TestClient) -> None:
    # Given
    from config import INTEREST_RATES
    from db.store import upsert_sgs

    for code in INTEREST_RATES.values():
        upsert_sgs(code, [{"data": "01/01/2026", "valor": "1.0"}])

    # When
    with patch("datafetchers.bcb_sgs.httpx.get") as provider_get:
        response = client.get("/api/juros")

    # Then
    assert response.status_code == 200
    assert all(response.json()["data"].values())
    provider_get.assert_not_called()


def test_focus_get_reads_database_without_provider_request(client: TestClient) -> None:
    # Given
    from db.store import upsert_focus

    for indicator in ("IPCA", "Selic", "PIB", "Câmbio", "IGP-M"):
        upsert_focus(indicator, [{"Data": "2026-01-01", "Mediana": 1.0}])

    # When
    with patch("datafetchers.focus.httpx.get") as provider_get:
        response = client.get("/api/focus")

    # Then
    assert response.status_code == 200
    assert all(response.json()["data"].values())
    provider_get.assert_not_called()


def test_focus_pib_uses_provider_indicator_pib_total() -> None:
    # Given
    from datafetchers.focus import fetch_focus

    provider_response = MagicMock()
    provider_response.json.return_value = {
        "value": [{"Data": "2026-01-01", "Indicador": "PIB Total", "Mediana": 2.0}],
    }
    provider_response.raise_for_status = MagicMock()

    # When
    with patch("datafetchers.focus.httpx.get", return_value=provider_response) as provider_get:
        result = fetch_focus("PIB", use_cache=False)

    # Then
    assert result
    assert provider_get.call_args.kwargs["params"]["$filter"] == "Indicador eq 'PIB Total'"


def test_titulos_get_reads_database_without_provider_request(client: TestClient) -> None:
    # Given
    from db.store import upsert_anbima

    upsert_anbima("historico", [{"Índice": "IMA-B", "Data de Referência": "2026-01-01"}])

    # When
    with patch("datafetchers.anbima.httpx.get") as provider_get:
        response = client.get("/api/titulos/nntn-b")

    # Then
    assert response.status_code == 200
    assert response.json()["data"]
    provider_get.assert_not_called()


def test_curvas_get_reads_stored_dates_without_provider_request(client: TestClient) -> None:
    # Given
    from db.store import upsert_b3_di

    upsert_b3_di([
        {"trade_date": "2026-08-13", "symbol": "DI1U26", "maturity": "2026-09-01", "rate": 13.9},
        {"trade_date": "2026-08-14", "symbol": "DI1V26", "maturity": "2026-10-01", "rate": 13.8},
    ])

    # When
    with patch("datafetchers.b3_di.httpx.get") as provider_get:
        response = client.get("/api/curvas-di?days=5")

    # Then
    assert response.status_code == 200
    assert response.json()["dates"] == ["13/08/2026", "14/08/2026"]
    provider_get.assert_not_called()


def test_eleicoes_get_reads_stale_cache_without_provider_request(client: TestClient) -> None:
    # Given
    from datafetchers.polymarket import CACHE_KEY
    from db.store import set_meta

    cached = {
        "source": "Polymarket",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "national": {
            "candidates": [{"name": "Candidate", "price": 0.5, "volume": 1}],
            "history": {"2026-08-01": {"Candidate": 50.0}},
        },
        "days": ["2026-08-01"],
        "ufs": [{"uf": "SP", "candidates": [{"name": "Candidate", "price": 0.5, "volume": 1}], "history": {}}],
    }
    set_meta(CACHE_KEY, json.dumps(cached))
    set_meta(f"{CACHE_KEY}_ts", "2026-08-01T00:00:00")

    # When
    with patch("datafetchers.polymarket.httpx.get") as provider_get:
        response = client.get("/api/eleicoes/estados")

    # Then
    assert response.status_code == 200
    assert response.json() == cached
    provider_get.assert_not_called()


def test_daily_refresh_covers_every_provider() -> None:
    # Given
    from main import daily_refresh

    successful_result: dict[str, list[dict[str, str]]] = {"series": [{"data": "01/01/2026", "valor": "1"}]}

    # When
    with (
        patch("main.fetch_sgs_batch", return_value=successful_result) as refresh_sgs,
        patch("main.fetch_all_focus", return_value=successful_result),
        patch("main.fetch_anbima_ima", return_value=successful_result),
        patch("main.fetch_di_curves", return_value=successful_result),
        patch("main.fetch_state_polls", return_value={"ufs": [{}] * 27, "days": ["2026-01-01"]}),
        patch("main.refresh_treasury_data", create=True, return_value={"quotes": 1, "catalog": 1}) as refresh_tesouro,
    ):
        result = daily_refresh(force=True)

    # Then
    assert refresh_sgs.call_count == 9
    refresh_tesouro.assert_called_once()
    assert result["status"] == "ok"
    assert len(result["tasks"]) == 14


def test_missing_sgs_cache_returns_service_unavailable() -> None:
    from main import app

    with (
        patch("routes.juros.read_sgs_batch", return_value={}),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get("/api/juros")

    assert response.status_code == 503
    assert response.json() == {"error": "dataset_unavailable", "dataset": "juros"}


def test_missing_election_cache_returns_service_unavailable() -> None:
    from main import app

    with (
        patch("routes.eleicoes.read_state_polls", return_value={"days": [], "ufs": []}),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get("/api/eleicoes/estados")

    assert response.status_code == 503
    assert response.json() == {"error": "dataset_unavailable", "dataset": "eleicoes.days"}


def test_missing_national_election_cache_returns_service_unavailable() -> None:
    from main import app

    incomplete = {
        "national": {"candidates": [], "history": {}},
        "days": ["2026-08-01"],
        "ufs": [{"uf": "SP"}],
    }
    with (
        patch("routes.eleicoes.read_state_polls", return_value=incomplete),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get("/api/eleicoes/estados")

    assert response.status_code == 503
    assert response.json() == {
        "error": "dataset_unavailable",
        "dataset": "eleicoes.national.candidates",
    }


@pytest.mark.parametrize(
    ("path", "dataset"),
    [
        ("/api/juros/selic-meta", "juros.selic_meta"),
        ("/api/juros/selic-efetiva", "juros.selic_efetiva"),
        ("/api/juros/cdi", "juros.cdi"),
        ("/api/juros/tr", "juros.tr"),
        ("/api/inflacao/ipca", "inflacao.ipca"),
        ("/api/inflacao/ipca-12m", "inflacao.ipca_12m"),
        ("/api/inflacao/igpm", "inflacao.igpm"),
        ("/api/atividade/pib", "atividade.pib"),
        ("/api/atividade/ibc-br", "atividade.ibc_br"),
        ("/api/atividade/desemprego", "atividade.desemprego"),
        ("/api/cambio/usd", "cambio.usd.compra"),
        ("/api/cambio/eur", "cambio.eur"),
        ("/api/complementares/reservas", "complementares.reservas_internacionais"),
        ("/api/complementares/base-monetaria", "complementares.base_monetaria"),
        ("/api/complementares/ic-commodities", "complementares.ic_commodities"),
        ("/api/focus/IPCA", "focus.IPCA"),
        ("/api/ipca-decomposicao/grupos", "ipca.grupos"),
        ("/api/ipca-decomposicao/naturezas", "ipca.naturezas"),
        ("/api/ipca-decomposicao/core", "ipca.nucleos"),
        ("/api/ipca-decomposicao/precos", "ipca.precos"),
    ],
)
def test_missing_individual_cache_returns_service_unavailable(
    client: TestClient,
    path: str,
    dataset: str,
) -> None:
    response = client.get(path)

    assert response.status_code == 503
    assert response.json() == {"error": "dataset_unavailable", "dataset": dataset}
