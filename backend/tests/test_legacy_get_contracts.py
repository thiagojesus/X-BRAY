import json
from pathlib import Path
from typing import TypedDict

from fastapi.routing import APIRoute

from main import app


class LegacyRoute(TypedDict):
    path: str
    parameters: list[str]


def _fixture() -> list[LegacyRoute]:
    path = Path(__file__).parent / "fixtures" / "contracts" / "legacy_get_contracts.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_frozen_legacy_get_contract_contains_all_41_routes() -> None:
    expected = _fixture()
    assert len(expected) == 41
    assert len({route["path"] for route in expected}) == 41


def test_application_preserves_every_frozen_legacy_get_route_and_parameter() -> None:
    expected = {route["path"]: route["parameters"] for route in _fixture()}
    actual = {
        route.path: sorted(parameter.name for parameter in route.dependant.query_params + route.dependant.path_params)
        for route in app.routes
        if isinstance(route, APIRoute) and "GET" in route.methods and route.path in expected
    }
    assert actual == {path: sorted(parameters) for path, parameters in expected.items()}


def test_quality_and_refresh_status_are_additive_not_legacy_contracts() -> None:
    paths = {route["path"] for route in _fixture()}
    assert "/api/data-quality/{dataset}" not in paths
    assert "/api/refresh-status" not in paths
