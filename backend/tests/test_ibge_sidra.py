import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from datafetchers.ibge_sidra import (
    MAX_RESPONSE_BYTES,
    SIDRA_DATASETS,
    TIMEOUT_SECONDS,
    SidraPayloadError,
    build_sidra_url,
    fetch_sidra_dataset,
    parse_sidra_payload,
)
from provider_http import ProviderPayloadTooLargeError

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sidra"

EXPECTED_IDS = {
    "pim_industria": (8888, 12607, 544, 129314),
    "pmc_varejo_ampliado": (8881, 7170, 11046, 56736),
    "pms_servicos": (5906, 7168, 11046, 56726),
    "massa_rendimento_real": (6392, 6293, None, None),
    "taxa_subutilizacao": (6441, 4118, None, None),
}


def _fixture(key: str) -> bytes:
    return (FIXTURE_DIR / f"{key}.json").read_bytes()


class FakeResponse:
    def __init__(self, content: bytes, url: str, status_code: int = 200) -> None:
        self.content = content
        self.url = url
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", self.url)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("provider error", request=request, response=response)


def test_catalog_contains_only_the_five_approved_brazil_specs() -> None:
    assert set(SIDRA_DATASETS) == set(EXPECTED_IDS)
    for key, expected in EXPECTED_IDS.items():
        spec = SIDRA_DATASETS[key]
        assert (spec.table_id, spec.variable_id, spec.classification_id, spec.category_id) == expected
        assert spec.territory_level == "N1"
        assert spec.territory_code == "1"


def test_builds_exact_sidra_urls_and_dimensions() -> None:
    assert build_sidra_url(SIDRA_DATASETS["pim_industria"]) == (
        "https://apisidra.ibge.gov.br/values/t/8888/n1/1/v/12607/p/all/c544/129314?formato=json"
    )
    assert build_sidra_url(SIDRA_DATASETS["massa_rendimento_real"]) == (
        "https://apisidra.ibge.gov.br/values/t/6392/n1/1/v/6293/p/all?formato=json"
    )


@pytest.mark.parametrize("key", tuple(EXPECTED_IDS))
def test_parses_minimized_official_payload_with_full_metadata(key: str) -> None:
    spec = SIDRA_DATASETS[key]

    observations = parse_sidra_payload(
        spec,
        _fixture(key),
        first_seen_at="2026-09-27T12:00:00+00:00",
    )

    assert len(observations) == 1
    observation = observations[0]
    assert observation["dataset_key"] == key
    assert observation["table_id"] == spec.table_id
    assert observation["variable_id"] == spec.variable_id
    assert observation["classification_id"] == spec.classification_id
    assert observation["category_id"] == spec.category_id
    assert observation["territory_level"] == "N1"
    assert observation["territory_code"] == "1"
    assert observation["reference_period"] == "202607"
    assert observation["reference_label"]
    assert observation["unit"] == spec.unit
    assert observation["base_version"] == spec.base_version
    assert observation["source_published_at"] is None
    assert observation["first_seen_at"] == "2026-09-27T12:00:00+00:00"
    assert isinstance(observation["value"], float)


def test_moving_quarter_payload_uses_ending_month_label_not_calendar_quarter() -> None:
    observation = parse_sidra_payload(
        SIDRA_DATASETS["massa_rendimento_real"],
        _fixture("massa_rendimento_real"),
        first_seen_at="2026-09-27T12:00:00+00:00",
    )[0]

    assert observation["reference_period"] == "202607"
    assert observation["reference_label"] == "mai-jun-jul 2026"
    assert observation["frequency"] == "moving_quarter"
    assert "trimestre" not in observation["reference_label"].lower()


def test_skips_sidra_unavailable_values_with_blank_unit_and_keeps_valid_periods() -> None:
    payload: list[dict[str, Any]] = json.loads(_fixture("taxa_subutilizacao"))
    template = payload[1]
    payload = [payload[0]]
    for period, value, unit in (
        ("201509", "20.8", "2"),
        ("201510", "...", ""),
        ("201511", "...", ""),
        ("201512", "20.4", "2"),
        ("202608", "13.1", "2"),
    ):
        row = dict(template)
        row.update(
            {
                "D3C": period,
                "D3N": f"período {period}",
                "V": value,
                "MC": unit,
                "MN": "" if not unit else "%",
            }
        )
        payload.append(row)

    observations = parse_sidra_payload(
        SIDRA_DATASETS["taxa_subutilizacao"],
        json.dumps(payload).encode(),
        first_seen_at="2026-10-21T12:00:00+00:00",
    )

    assert [item["reference_period"] for item in observations] == [
        "201509",
        "201512",
        "202608",
    ]
    assert [item["value"] for item in observations] == [20.8, 20.4, 13.1]


@pytest.mark.parametrize(
    ("raw_value", "territory"),
    [("not-reported", "1"), ("...", "35")],
)
def test_blank_unit_rows_are_not_silently_ignored_unless_valid_missing_marker_and_dimensions(
    raw_value: str, territory: str
) -> None:
    payload: list[dict[str, Any]] = json.loads(_fixture("taxa_subutilizacao"))
    payload[1].update({"MC": "", "V": raw_value, "D1C": territory})

    with pytest.raises(SidraPayloadError):
        parse_sidra_payload(
            SIDRA_DATASETS["taxa_subutilizacao"],
            json.dumps(payload).encode(),
            first_seen_at="2026-09-27T12:00:00+00:00",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("D1C", "35"),
        ("D2C", "99999"),
        ("D3C", "2026-Q3"),
        ("D4C", "99999"),
        ("MC", "999"),
        ("V", "NaN"),
    ],
)
def test_rejects_invalid_schema_dimensions_and_values(field: str, value: str) -> None:
    payload: list[dict[str, Any]] = json.loads(_fixture("pim_industria"))
    payload[1][field] = value

    with pytest.raises(SidraPayloadError):
        parse_sidra_payload(
            SIDRA_DATASETS["pim_industria"],
            json.dumps(payload).encode(),
            first_seen_at="2026-09-27T12:00:00+00:00",
        )


@pytest.mark.parametrize("reference_period", ["202600", "202613", "000001"])
def test_rejects_invalid_calendar_period(reference_period: str) -> None:
    payload: list[dict[str, Any]] = json.loads(_fixture("pim_industria"))
    payload[1]["D3C"] = reference_period

    with pytest.raises(SidraPayloadError, match="reference period"):
        parse_sidra_payload(
            SIDRA_DATASETS["pim_industria"],
            json.dumps(payload).encode(),
            first_seen_at="2026-09-27T12:00:00+00:00",
        )


def test_fetch_uses_30_second_timeout_and_five_mib_limit() -> None:
    calls: list[tuple[float, bool]] = []

    def request(
        url: str,
        *,
        timeout: float,
        follow_redirects: bool,
        headers: Mapping[str, str] | None,
        params: Mapping[str, str] | None,
    ) -> FakeResponse:
        del headers, params
        calls.append((timeout, follow_redirects))
        return FakeResponse(_fixture("pim_industria"), url)

    observations = fetch_sidra_dataset(
        SIDRA_DATASETS["pim_industria"],
        request=request,
        sleep=lambda _seconds: None,
        jitter=lambda: 0,
    )

    assert observations
    assert calls == [(TIMEOUT_SECONDS, False)]
    assert MAX_RESPONSE_BYTES == 5 * 1024 * 1024


def test_retries_transport_and_429_before_success() -> None:
    calls = 0

    def request(url: str, **_kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("offline")
        status_code = 429 if calls == 2 else 200
        return FakeResponse(_fixture("pim_industria"), url, status_code)

    observations = fetch_sidra_dataset(
        SIDRA_DATASETS["pim_industria"],
        request=request,
        sleep=lambda _seconds: None,
        jitter=lambda: 0,
    )

    assert observations
    assert calls == 3


@pytest.mark.parametrize("status_code", [429, 503])
def test_red_xbr_402_retries_retryable_http_status(status_code: int) -> None:
    calls = 0

    def request(url: str, **_kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        response_status = status_code if calls == 1 else 200
        return FakeResponse(_fixture("pim_industria"), url, response_status)

    observations = fetch_sidra_dataset(
        SIDRA_DATASETS["pim_industria"],
        request=request,
        sleep=lambda _seconds: None,
        jitter=lambda: 0,
    )

    assert observations
    assert calls == 2


def test_red_xbr_402_caps_repeated_5xx_at_three_attempts() -> None:
    calls = 0

    def request(url: str, **_kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(_fixture("pim_industria"), url, 503)

    with pytest.raises(httpx.HTTPStatusError):
        fetch_sidra_dataset(
            SIDRA_DATASETS["pim_industria"],
            request=request,
            sleep=lambda _seconds: None,
            jitter=lambda: 0,
        )

    assert calls == 3


def test_does_not_retry_non_429_4xx() -> None:
    calls = 0

    def request(url: str, **_kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(b"{}", url, 400)

    with pytest.raises(httpx.HTTPStatusError):
        fetch_sidra_dataset(
            SIDRA_DATASETS["pim_industria"],
            request=request,
            sleep=lambda _seconds: None,
            jitter=lambda: 0,
        )
    assert calls == 1


def test_rejects_payload_larger_than_five_mib() -> None:
    def request(url: str, **_kwargs: object) -> FakeResponse:
        return FakeResponse(b"x" * (MAX_RESPONSE_BYTES + 1), url)

    with pytest.raises(ProviderPayloadTooLargeError):
        fetch_sidra_dataset(
            SIDRA_DATASETS["pim_industria"],
            request=request,
            sleep=lambda _seconds: None,
            jitter=lambda: 0,
        )
