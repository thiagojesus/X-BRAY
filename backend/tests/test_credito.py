import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from db import store
from db.store import query_sgs, upsert_sgs
from indicator_catalog import (
    INDICATOR_CATALOG,
    CatalogGroup,
    IndicatorKey,
    Provider,
    Unit,
    legacy_codes,
)
from main import app
from refresh import RefreshDependencies, daily_refresh


class CreditRecord(TypedDict):
    data: str
    valor: str


class CreditMetadata(TypedDict):
    code: int
    unit: str
    frequency: str
    reference_period: str
    deadline: str
    freshness: str


class CreditContract(TypedDict):
    data: dict[str, list[CreditRecord]]
    status: str
    unavailable: list[str]
    metadata: dict[str, CreditMetadata]


EXPECTED_CODES = {
    "inadimplencia_pf": 21084,
    "endividamento_familias": 29037,
    "comprometimento_renda": 29034,
    "icc_pf": 25353,
    "taxa_media_credito_pf": 20716,
    "carteira_pf_cheque_especial": 20573,
    "carteira_pf_credito_pessoal_nao_consignado": 20574,
    "carteira_pf_cartao_rotativo": 20587,
    "carteira_pf_cartao_parcelado": 20588,
}
EXPECTED_UNITS = {
    "inadimplencia_pf": Unit.PCT,
    "endividamento_familias": Unit.PCT,
    "comprometimento_renda": Unit.PCT,
    "icc_pf": Unit.PCT_ANNUAL,
    "taxa_media_credito_pf": Unit.PCT_ANNUAL,
    "carteira_pf_cheque_especial": Unit.BRL_MILLION,
    "carteira_pf_credito_pessoal_nao_consignado": Unit.BRL_MILLION,
    "carteira_pf_cartao_rotativo": Unit.BRL_MILLION,
    "carteira_pf_cartao_parcelado": Unit.BRL_MILLION,
}
EXPECTED_FRESHNESS_DAYS = {
    "inadimplencia_pf": 45,
    "endividamento_familias": 75,
    "comprometimento_renda": 75,
    "icc_pf": 45,
    "taxa_media_credito_pf": 45,
    "carteira_pf_cheque_especial": 45,
    "carteira_pf_credito_pessoal_nao_consignado": 45,
    "carteira_pf_cartao_rotativo": 45,
    "carteira_pf_cartao_parcelado": 45,
}
LEGACY_CODES = {name: code for name, code in EXPECTED_CODES.items()
                if name not in {
                    "carteira_pf_cheque_especial", "carteira_pf_credito_pessoal_nao_consignado",
                    "carteira_pf_cartao_rotativo", "carteira_pf_cartao_parcelado",
                }}


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "credito.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()

    yield

    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _contract() -> CreditContract:
    path = Path(__file__).parent / "fixtures" / "contracts" / "credito.json"
    return cast(CreditContract, json.loads(path.read_text(encoding="utf-8")))


def _seed(*, omit: str | None = None) -> None:
    for name, records in _contract()["data"].items():
        if name != omit:
            upsert_sgs(EXPECTED_CODES[name], records)


def _seed_modalities() -> None:
    for code in (20573, 20574, 20587, 20588):
        upsert_sgs(code, [
            {"data": "01/01/2025", "valor": "100.5"},
            {"data": "01/12/2025", "valor": "110.5"},
            {"data": "01/01/2026", "valor": "120.5"},
        ])


def test_red_xbr_201_catalog_has_exact_credit_series_units_and_slas() -> None:
    credit_group = getattr(CatalogGroup, "CREDIT", None)
    credit_codes = getattr(config, "CREDIT", None)
    assert credit_group is not None, "[RED XBR-201] missing credit catalog group"
    assert credit_codes == EXPECTED_CODES, "[RED XBR-201] credit codes drifted"
    assert legacy_codes(credit_group) == EXPECTED_CODES

    specs = [spec for spec in INDICATOR_CATALOG.values() if spec.group is credit_group]
    assert {str(spec.key) for spec in specs} == set(EXPECTED_CODES)
    for name, code in EXPECTED_CODES.items():
        spec = INDICATOR_CATALOG[IndicatorKey(name)]
        assert spec.provider is Provider.BCB_SGS
        assert spec.provider_code == code
        assert spec.unit is EXPECTED_UNITS[name]
        assert spec.freshness_days == EXPECTED_FRESHNESS_DAYS[name]


def test_red_xbr_201_ok_contract_preserves_reference_periods_and_annual_units() -> None:
    _seed()

    with (
        patch("datafetchers.bcb_sgs.httpx.get") as provider_get,
        patch("routes.credito.datetime") as clock,
    ):
        clock.now.return_value = datetime(2026, 9, 21, tzinfo=timezone.utc)
        response = TestClient(app).get("/api/credito")

    assert response.status_code == 200, "[RED XBR-201] credit GET route is missing"
    provider_get.assert_not_called()
    body = response.json()
    expected = _contract()
    assert {key: body["data"][key] for key in expected["data"]} == expected["data"]
    assert body["status"] == "partial"
    assert {key: body["metadata"][key] for key in expected["metadata"]} == expected["metadata"]
    assert set(body["modalities"]) == set(EXPECTED_CODES) - set(LEGACY_CODES)
    assert response.json()["data"]["taxa_media_credito_pf"][0]["valor"] == "14.2"
    assert {
        name: metadata["reference_period"]
        for name, metadata in response.json()["metadata"].items()
    } == {
        "inadimplencia_pf": "2026-08-01",
        "endividamento_familias": "2026-07-01",
        "comprometimento_renda": "2026-08-01",
        "icc_pf": "2026-08-01",
        "taxa_media_credito_pf": "2026-09-01",
    }


def test_red_xbr_201_partial_keeps_four_independent_series() -> None:
    _seed(omit="comprometimento_renda")

    response = TestClient(app).get("/api/credito")

    assert response.status_code == 200, "[RED XBR-201] partial data must remain usable"
    body = response.json()
    assert body["status"] == "partial"
    assert body["unavailable"] == [
        "comprometimento_renda", "carteira_pf_cheque_especial",
        "carteira_pf_credito_pessoal_nao_consignado", "carteira_pf_cartao_rotativo",
        "carteira_pf_cartao_parcelado",
    ]
    assert set(body["data"]) == set(EXPECTED_CODES) - {"comprometimento_renda"}


def test_red_xbr_201_zero_series_returns_typed_503() -> None:
    response = TestClient(app).get("/api/credito")

    assert response.status_code == 503, "[RED XBR-201] empty cache must be unavailable"
    assert response.json() == {"error": "dataset_unavailable", "dataset": "credito"}
    quality = TestClient(app).get("/api/data-quality/credito").json()
    assert quality["status"] == "unavailable"
    assert {item["series"] for item in quality["unavailable"]} == set(EXPECTED_CODES)


def test_credit_exposure_api_returns_cached_growth_and_keeps_legacy_raw_values() -> None:
    _seed()
    _seed_modalities()
    before = {code: query_sgs(code) for code in (20573, 20574, 20587, 20588)}

    def forbidden_write(*_args, **_kwargs):
        raise AssertionError("credit GET must not write cached/provenance state")

    with (
        patch("datafetchers.bcb_sgs.httpx.get") as provider_get,
        patch.object(store, "_execute", side_effect=forbidden_write),
        patch.object(store, "execute_transaction", side_effect=forbidden_write),
        patch.object(store, "execute_statement", side_effect=forbidden_write),
    ):
        client = TestClient(app)
        first = client.get("/api/credito")
        second = client.get("/api/credito")

    assert first.status_code == second.status_code == 200
    body = first.json()
    assert body == second.json()
    assert body["status"] == "ok"
    quality = TestClient(app).get("/api/data-quality/credito").json()
    assert quality["status"] == body["status"]
    assert quality["unavailable"] == []
    assert len(quality["metadata"]) == 9
    assert quality["last_updated"] == "2026-09-01"
    assert body["data"]["taxa_media_credito_pf"] == _contract()["data"]["taxa_media_credito_pf"]
    assert body["data"]["carteira_pf_cheque_especial"] == before[20573]
    modality = body["modalities"]["carteira_pf_cheque_especial"]
    assert modality["code"] == 20573
    assert modality["unit"] == "brl_million"
    assert modality["latest"] == {"reference": "2026-01-01", "value": 120.5, "reason": None}
    assert modality["mom"]["value_pct"] == pytest.approx((120.5 / 110.5 - 1) * 100)
    assert modality["mom"]["base_reference"] == "2025-12-01"
    assert modality["yoy"]["value_pct"] == pytest.approx((120.5 / 100.5 - 1) * 100)
    assert modality["yoy"]["base_reference"] == "2025-01-01"
    assert modality["history"][-1]["value"] == 120.5
    assert body["metadata"]["carteira_pf_cheque_especial"]["reference_period"] == "2026-01-01"
    assert all(body["modalities"][key]["source_url"].endswith(f"bcdata.sgs.{code}/dados")
               for key, code in zip(body["modalities"], (20573, 20574, 20587, 20588)))
    provider_get.assert_not_called()
    assert {code: query_sgs(code) for code in before} == before


def test_sgs_producer_fixture_persists_modalities_then_readonly_get_projects_them() -> None:
    from datafetchers import bcb_sgs

    codes = {name: EXPECTED_CODES[name] for name in EXPECTED_CODES if name not in LEGACY_CODES}
    source_rows = [
        {"data": "01/01/2025", "valor": "100.5"},
        {"data": "01/12/2025", "valor": "110.5"},
        {"data": "01/01/2026", "valor": "120.5"},
    ]

    def local_sgs_fixture(url, _params, _timeout, _sink):
        assert any(f"bcdata.sgs.{code}/dados" in url for code in codes.values())
        return source_rows

    with patch.object(bcb_sgs, "_fetch_sgs_json", side_effect=local_sgs_fixture):
        fetched = bcb_sgs.fetch_sgs_batch(
            codes, start_date="01/01/2025", end_date="31/01/2026",
            use_cache=False, mark_refreshed=False, fail_fast=True,
        )
    assert set(fetched) == set(codes)
    assert all(query_sgs(code) for code in codes.values())

    def forbidden_http(*_args, **_kwargs):
        raise AssertionError("GET must not use provider HTTP")

    with patch("datafetchers.bcb_sgs.httpx.get", side_effect=forbidden_http):
        response = TestClient(app).get("/api/credito")
    assert response.status_code == 200
    body = response.json()
    assert all(not body["modalities"][name]["unavailable"] for name in codes)
    assert body["modalities"]["carteira_pf_cheque_especial"]["mom"]["value_pct"] == pytest.approx(
        (120.5 / 110.5 - 1) * 100
    )


@pytest.mark.parametrize("available_modalities", [0, 1, 3])
def test_missing_exposure_modalities_are_partial_without_blanking_legacy_or_siblings(
    available_modalities: int,
) -> None:
    _seed()
    for code in (20573, 20574, 20587, 20588)[:available_modalities]:
        upsert_sgs(code, [{"data": "01/01/2026", "valor": "12.5"}])

    response = TestClient(app).get("/api/credito")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert set(body["data"]) >= set(LEGACY_CODES)
    assert body["data"]["taxa_media_credito_pf"] == _contract()["data"]["taxa_media_credito_pf"]
    assert sum(not item["unavailable"] for item in body["modalities"].values()) == available_modalities
    assert len(body["unavailable"]) == len(LEGACY_CODES) - 5 + 4 - available_modalities
    quality = TestClient(app).get("/api/data-quality/credito").json()
    assert quality["status"] == body["status"]
    assert quality["unavailable"] == [
        {"series": name, "required": True, "reason": "no_valid_observations"}
        for name in (
            "carteira_pf_cheque_especial",
            "carteira_pf_credito_pessoal_nao_consignado",
            "carteira_pf_cartao_rotativo",
            "carteira_pf_cartao_parcelado",
        )[available_modalities:]
    ]
    assert len(quality["metadata"]) == 5 + available_modalities


def test_only_new_modalities_still_return_partial_cached_dataset() -> None:
    _seed_modalities()
    response = TestClient(app).get("/api/credito")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert set(body["data"]) == set(EXPECTED_CODES) - set(LEGACY_CODES)
    assert all(not modality["unavailable"] for modality in body["modalities"].values())
    quality = TestClient(app).get("/api/data-quality/credito").json()
    assert quality["status"] == body["status"]
    assert {item["series"] for item in quality["unavailable"]} == set(LEGACY_CODES)
    assert len(quality["metadata"]) == 4


def test_route_as_of_before_and_after_point_controls_validity_and_catalog_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    from types import MappingProxyType

    _seed()
    upsert_sgs(20573, [{"data": "31/01/2026", "valor": "15"}])
    response_before = None
    with patch("routes.credito.datetime") as clock:
        clock.now.return_value = datetime(2025, 12, 31, tzinfo=timezone.utc)
        response_before = TestClient(app).get("/api/credito")
    assert response_before is not None and response_before.status_code == 200, response_before.text
    assert response_before.json()["modalities"]["carteira_pf_cheque_especial"]["unavailable"] is True

    catalog = dict(INDICATOR_CATALOG)
    key = IndicatorKey("carteira_pf_cheque_especial")
    catalog[key] = replace(catalog[key], freshness_days=2)
    monkeypatch.setattr("routes.credito.INDICATOR_CATALOG", MappingProxyType(catalog))
    with patch("routes.credito.datetime") as clock:
        clock.now.return_value = datetime(2026, 3, 1, tzinfo=timezone.utc)
        response_after = TestClient(app).get("/api/credito")
    assert response_after.status_code == 200
    modality = response_after.json()["modalities"]["carteira_pf_cheque_especial"]
    assert modality["unavailable"] is False, modality
    assert modality["latest"]["reference"] == "2026-01-31"
    assert response_after.json()["metadata"]["carteira_pf_cheque_especial"]["deadline"] == "2026-02-02"
    assert response_after.json()["metadata"]["carteira_pf_cheque_especial"]["freshness"] == "stale"


def test_one_invalid_modality_does_not_blank_three_siblings_or_legacy() -> None:
    _seed()
    _seed_modalities()
    store._execute("DELETE FROM sgs WHERE series_code = ?", (20573,))
    upsert_sgs(20573, [{"data": "2026-01-01", "valor": "Infinity"}])
    response = TestClient(app).get("/api/credito")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["modalities"]["carteira_pf_cheque_especial"]["unavailable"] is True
    assert sum(not modality["unavailable"] for modality in body["modalities"].values()) == 3
    assert body["data"]["taxa_media_credito_pf"] == _contract()["data"]["taxa_media_credito_pf"]
    quality = TestClient(app).get("/api/data-quality/credito").json()
    assert quality["status"] == body["status"]
    assert {item["series"]: item["reason"] for item in quality["unavailable"]}[
        "carteira_pf_cheque_especial"
    ] == body["modalities"]["carteira_pf_cheque_especial"]["latest"]["reason"]


def test_modality_bad_structure_isolated_from_cached_legacy_series() -> None:
    _seed()
    batch = {name: records for name, records in _contract()["data"].items()}
    batch.update({"carteira_pf_cheque_especial": None})
    for key in EXPECTED_CODES.keys() - LEGACY_CODES.keys() - {"carteira_pf_cheque_especial"}:
        batch[key] = []
    with patch("routes.credito.query_sgs_batch", return_value=batch):
        response = TestClient(app).get("/api/credito")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["data"]["taxa_media_credito_pf"] == _contract()["data"]["taxa_media_credito_pf"]
    assert body["modalities"]["carteira_pf_cheque_especial"]["latest"]["reason"] == "invalid_current"


def test_red_xbr_201_stale_series_remains_visible_with_own_deadline() -> None:
    upsert_sgs(21084, [{"data": "01/01/2020", "valor": "4.1"}])

    response = TestClient(app).get("/api/credito")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["data"]["inadimplencia_pf"] == [{"data": "01/01/2020", "valor": "4.1"}]
    assert body["metadata"]["inadimplencia_pf"] == {
        "code": 21084,
        "unit": "pct",
        "frequency": "monthly",
        "reference_period": "2020-01-01",
        "deadline": "2020-03-16",
        "freshness": "stale",
    }


def test_red_xbr_201_malformed_batch_is_rejected() -> None:
    malformed = {name: [None, {"data": "01/08/2026", "valor": "1.0"}] for name in EXPECTED_CODES}

    with patch("routes.credito.query_sgs_batch", return_value=malformed):
        response = TestClient(app).get("/api/credito")

    assert response.status_code == 200, "usable modality observations remain independently available"
    assert response.json()["status"] == "partial"
    assert response.json()["data"]["carteira_pf_cheque_especial"]


def test_red_xbr_201_refresh_requires_bearer_auth_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "server-secret")

    response = TestClient(app).post(
        "/api/credito/refresh",
        headers={"Authorization": "Bearer wrong-secret"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "unauthorized"


def test_red_xbr_201_refresh_preserves_last_good_cache_and_logs_provider_failure() -> None:
    _seed()

    with patch(
        "datafetchers.bcb_sgs.get_provider_response",
        side_effect=RuntimeError("provider unavailable"),
    ) as provider_get:
        response = TestClient(app).post("/api/credito/refresh")

    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    assert provider_get.call_count >= len(EXPECTED_CODES)
    assert query_sgs(20716) == [{"data": "01/09/2026", "valor": "14.2"}]


def test_red_xbr_201_daily_refresh_registers_credit_sgs_task() -> None:
    sgs_calls: list[tuple[dict[str, int], str | None, bool]] = []

    def fetch_sgs_batch(
        codes: dict[str, int],
        *,
        start_date: str | None = None,
        use_cache: bool = True,
    ) -> dict[str, list[dict[str, str]]]:
        sgs_calls.append((codes, start_date, use_cache))
        return {name: [{"data": "01/08/2026", "valor": "1.0"}] for name in codes}

    payload = {"data": [{"value": 1}]}
    dependencies = RefreshDependencies(
        fetch_sgs_batch=fetch_sgs_batch,
        fetch_all_focus=lambda **_kwargs: payload,
        fetch_focus_12m=lambda **_kwargs: payload,
        fetch_anbima_ima=lambda **_kwargs: payload,
        fetch_di_curves=lambda **_kwargs: payload,
        fetch_state_polls=lambda **_kwargs: payload,
        refresh_treasury_data=lambda: payload,
        refresh_sidra_data=lambda: payload,
    )

    result = daily_refresh(force=True, dependencies=dependencies)

    assert (EXPECTED_CODES, "01/01/2011", False) in sgs_calls
    credit_tasks = [task for task in result["tasks"] if task["name"] == "sgs_credito"]
    assert len(credit_tasks) == 1, "[RED XBR-201] refresh must expose sgs_credito"
    assert credit_tasks[0]["status"] == "ok"
