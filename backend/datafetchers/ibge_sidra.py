import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from types import MappingProxyType
from typing import Final, Literal, TypedDict

import httpx
from pydantic import TypeAdapter, ValidationError

from db.sidra_store import insert_sidra_observations
from provider_http import ProviderRequest, get_provider_response
from source_ingestion import REGISTRY

SIDRA_BASE_URL: Final = "https://apisidra.ibge.gov.br/values"
TIMEOUT_SECONDS: Final = 30.0
MAX_RESPONSE_BYTES: Final = 5 * 1024 * 1024
MAX_ATTEMPTS: Final = 3
_PERIOD_PATTERN: Final = re.compile(r"^\d{6}$")
_MISSING_VALUES: Final = frozenset({"", "-", "...", "X"})
Frequency = Literal["monthly", "moving_quarter"]
SidraRawRow = dict[str, str]
_JSON_ADAPTER: Final[TypeAdapter[list[SidraRawRow]]] = TypeAdapter(list[SidraRawRow])


@dataclass(frozen=True, slots=True)
class SidraDatasetSpec:
    key: str
    table_id: int
    variable_id: int
    classification_id: int | None
    category_id: int | None
    table_label: str
    variable_label: str
    classification_label: str | None
    category_label: str | None
    unit: str
    unit_code: str
    unit_label: str
    base_version: str | None
    frequency: Frequency
    territory_level: str = "N1"
    territory_code: str = "1"


class SidraObservation(TypedDict):
    dataset_key: str
    table_id: int
    variable_id: int
    classification_id: int | None
    category_id: int | None
    territory_level: str
    territory_code: str
    reference_period: str
    reference_label: str
    value: float
    unit: str
    base_version: str | None
    source_published_at: None
    first_seen_at: str
    frequency: Frequency


class SidraPayloadError(ValueError):
    pass


class SidraRetryStateError(RuntimeError):
    pass


_SPECS: Final = (
    SidraDatasetSpec("pim_industria", 8888, 12607, 544, 129314, "Produção Física Industrial, por seções e atividades industriais", "PIMPF - Número-índice com ajuste sazonal (2022=100)", "Seções e atividades industriais (CNAE 2.0)", "1 Indústria geral", "index", "30", "Número-índice", "2022=100", "monthly"),
    SidraDatasetSpec("pmc_varejo_ampliado", 8881, 7170, 11046, 56736, "Índice e variação da receita nominal e do volume de vendas no comércio varejista ampliado (2022=100)", "PMC - Número-índice com ajuste sazonal (2022=100)", "Tipos de índice", "Índice de volume de vendas no comércio varejista ampliado", "index", "30", "Número-índice", "2022=100", "monthly"),
    SidraDatasetSpec("pms_servicos", 5906, 7168, 11046, 56726, "Índice e variação da receita nominal e do volume de serviços (2022=100)", "PMS - Número-índice com ajuste sazonal (2022=100)", "Tipos de índice", "Índice de volume de serviços", "index", "30", "Número-índice", "2022=100", "monthly"),
    SidraDatasetSpec("massa_rendimento_real", 6392, 6293, None, None, "Massa de rendimento mensal real e nominal", "Massa de rendimento mensal real das pessoas de 14 anos ou mais", None, None, "brl_million", "1550", "Milhões de Reais", "real_deflator_latest_published_moving_quarter", "moving_quarter"),
    SidraDatasetSpec("taxa_subutilizacao", 6441, 4118, None, None, "Taxa composta da subutilização da força de trabalho", "Taxa composta de subutilização da força de trabalho", None, None, "pct", "2", "Percentual", None, "moving_quarter"),
)

SIDRA_DATASETS: Final[Mapping[str, SidraDatasetSpec]] = MappingProxyType(
    {spec.key: spec for spec in _SPECS}
)


def build_sidra_url(spec: SidraDatasetSpec, period: str = "all") -> str:
    path = (
        f"{SIDRA_BASE_URL}/t/{spec.table_id}/n1/{spec.territory_code}"
        f"/v/{spec.variable_id}/p/{period}"
    )
    if spec.classification_id is not None and spec.category_id is not None:
        path += f"/c{spec.classification_id}/{spec.category_id}"
    return f"{path}?formato=json"


def _text(row: Mapping[str, str], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise SidraPayloadError(f"SIDRA field {key} must be text")
    return value


def _validate_header(header: Mapping[str, str], classified: bool) -> None:
    required = {"NC", "NN", "MC", "MN", "V", "D1C", "D1N", "D2C", "D2N", "D3C", "D3N"}
    if classified:
        required.update({"D4C", "D4N"})
    if not required.issubset(header):
        raise SidraPayloadError("SIDRA header is incomplete")
    if _text(header, "V") != "Valor":
        raise SidraPayloadError("SIDRA header does not identify values")


def _parse_value(value: str) -> float | None:
    if value in _MISSING_VALUES:
        return None
    try:
        parsed = float(value.replace(",", "."))
    except ValueError as exc:
        raise SidraPayloadError("SIDRA value is not numeric") from exc
    if not isfinite(parsed):
        raise SidraPayloadError("SIDRA value is not finite")
    return parsed


def _parse_row(
    spec: SidraDatasetSpec,
    row: Mapping[str, str],
    first_seen_at: str,
) -> SidraObservation | None:
    if _text(row, "NC") != "1" or _text(row, "D1C") != spec.territory_code:
        raise SidraPayloadError("SIDRA territory does not match Brazil N1")
    if _text(row, "NN") != "Brasil" or _text(row, "D1N") != "Brasil":
        raise SidraPayloadError("SIDRA territory label does not match Brazil")
    if _text(row, "D2C") != str(spec.variable_id):
        raise SidraPayloadError("SIDRA variable does not match the dataset spec")
    period = _text(row, "D3C")
    if _PERIOD_PATTERN.fullmatch(period) is None:
        raise SidraPayloadError("SIDRA reference period must use AAAAMM")
    try:
        datetime(int(period[:4]), int(period[4:]), 1, tzinfo=timezone.utc)
    except ValueError as exc:
        raise SidraPayloadError("SIDRA reference period must be a valid AAAAMM month") from exc
    period_label = _text(row, "D3N")
    if not period_label:
        raise SidraPayloadError("SIDRA reference period label is missing")
    if spec.category_id is not None and _text(row, "D4C") != str(spec.category_id):
        raise SidraPayloadError("SIDRA category does not match the dataset spec")
    raw_value = _text(row, "V")
    unit_code = _text(row, "MC")
    if not unit_code and raw_value == "...":
        return None
    if unit_code != spec.unit_code:
        raise SidraPayloadError("SIDRA unit does not match the dataset spec")
    value = _parse_value(raw_value)
    if value is None:
        return None
    return {
        "dataset_key": spec.key,
        "table_id": spec.table_id,
        "variable_id": spec.variable_id,
        "classification_id": spec.classification_id,
        "category_id": spec.category_id,
        "territory_level": spec.territory_level,
        "territory_code": spec.territory_code,
        "reference_period": period,
        "reference_label": period_label,
        "value": value,
        "unit": spec.unit,
        "base_version": spec.base_version,
        "source_published_at": None,
        "first_seen_at": first_seen_at,
        "frequency": spec.frequency,
    }


def parse_sidra_payload(
    spec: SidraDatasetSpec,
    content: bytes,
    *,
    first_seen_at: str,
) -> list[SidraObservation]:
    try:
        payload = _JSON_ADAPTER.validate_json(content)
    except ValidationError as exc:
        raise SidraPayloadError("SIDRA payload is not valid JSON") from exc
    if len(payload) < 2:
        raise SidraPayloadError("SIDRA payload must contain a header and observations")
    _validate_header(payload[0], spec.category_id is not None)
    observations = [
        observation
        for row in payload[1:]
        if (observation := _parse_row(spec, row, first_seen_at)) is not None
    ]
    if not observations:
        raise SidraPayloadError("SIDRA payload contains no usable observations")
    return observations


def fetch_sidra_dataset(
    spec: SidraDatasetSpec,
    *,
    request: ProviderRequest | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
) -> list[SidraObservation]:
    url = build_sidra_url(spec)
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = (
                get_provider_response(
                    url,
                    request=request,
                    timeout=TIMEOUT_SECONDS,
                    max_bytes=MAX_RESPONSE_BYTES,
                    follow_redirects=True,
                )
                if request is not None
                else get_provider_response(
                    url,
                    timeout=TIMEOUT_SECONDS,
                    max_bytes=MAX_RESPONSE_BYTES,
                    follow_redirects=True,
                )
            )
            seen_at = datetime.now(timezone.utc).isoformat()
            return parse_sidra_payload(spec, response.content, first_seen_at=seen_at)
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
            if not retryable or attempt + 1 == MAX_ATTEMPTS:
                raise
        except httpx.TransportError:
            if attempt + 1 == MAX_ATTEMPTS:
                raise
        sleep((2**attempt) + jitter())
    raise SidraRetryStateError("unreachable SIDRA retry state")


def refresh_sidra_data() -> dict[str, list[SidraObservation]]:
    refreshed: dict[str, list[SidraObservation]] = {}
    for key, spec in SIDRA_DATASETS.items():
        try:
            def fetch_and_persist(dataset_spec: SidraDatasetSpec = spec) -> list[SidraObservation]:
                observations = fetch_sidra_dataset(dataset_spec)
                insert_sidra_observations(observations)
                return observations

            observations = REGISTRY["ibge_sidra"].fetch(
                indicator=key,
                qualifiers={"table_id": spec.table_id, "variable_id": spec.variable_id,
                            "frequency": spec.frequency},
                operation=fetch_and_persist,
                network_expected=True,
            )
            refreshed[key] = observations
        except (httpx.HTTPError, SidraPayloadError, ValueError):
            refreshed[key] = []
    return refreshed
