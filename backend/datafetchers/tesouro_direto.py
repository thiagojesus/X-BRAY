import csv
import hashlib
import json
from datetime import datetime, timezone
from io import StringIO
from time import monotonic
from typing import NotRequired, TypedDict
from uuid import uuid4

from datafetchers.tesouro_bonds import (
    ALLOWED_BOND_TYPES,
    _bond_code,  # noqa: F401 - kept as a compatibility export used by existing callers.
    _get_maturity_label,
    _parse_date,
    _parse_rate,
    build_bond_catalog,
    build_bond_history,
    build_bonds_history,
)
from db import store
from db.store import (
    TreasuryCohortIntent,
    query_treasury_latest_rows,
    query_treasury_rows,
)
from db.treasury_store import (
    TREASURY_NETWORK_BUDGET_SECONDS,
    TREASURY_OUTCOME_RESERVE_SECONDS,
    TREASURY_POST_NETWORK_RESERVE_SECONDS,
    TREASURY_TOTAL_BUDGET_SECONDS,
    active_treasury_cohort_fingerprint,
    persist_treasury_rows,
    prepare_treasury_rows,
    validate_treasury_csv,
)
from ingestion_policy import current_retry_budget
from provider_http import BINARY_MAX_BYTES, get_provider_response_bounded
from source_contracts import TREASURY_CONTRACT
from source_ingestion import (
    FetchResult,
    acquisition_unit,
    audited,
    current_extraction_id,
    current_refresh_context,
    record_validated_network_receipt,
)

TESOURO_TRANSPARENTE_CSV = "https://www.tesourotransparente.gov.br/ckan/dataset/taxas-dos-titulos-ofertados-pelo-tesouro-direto/resource/796d2059-14e9-44e3-80c9-2d9e30b405c1/download"
TESOURO_DATASET_UUID = "796d2059-14e9-44e3-80c9-2d9e30b405c1"
_TREASURY_COHORT_SCOPE_SHA256 = hashlib.sha256(json.dumps({
    "dataset_uuid": TESOURO_DATASET_UUID,
    "contract_id": TREASURY_CONTRACT.contract_id,
    "contract_version": TREASURY_CONTRACT.schema_version,
    "intent": "complete_daily_offered_titles_and_quote_sides",
}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class TreasuryReceiptReconciliationRequired(RuntimeError):
    """A matching old receipt exists but the active business cohort disagrees."""


def _v6_treasury_intent() -> TreasuryCohortIntent | None:
    run_id, task_key, _cycle_id = current_refresh_context()
    if run_id is None or task_key != "tesouro":
        return None
    rows = store.query_rows(
        "SELECT manifest_version FROM refresh_runs WHERE run_id = ?", (run_id,)
    )
    if not rows or rows[0].get("manifest_version") != "daily-refresh-v6":
        return None
    return TreasuryCohortIntent(
        run_id, task_key, TREASURY_CONTRACT.schema_version,
        TESOURO_DATASET_UUID, _TREASURY_COHORT_SCOPE_SHA256, "",
    )


def _replay_v6_treasury_receipt(intent: TreasuryCohortIntent) -> FetchResult | None:
    bound = store.treasury_cohort_commit_for_intent(intent)
    if bound is None:
        return None
    receipt, stored_rows_sha256 = bound
    current_sha256, current_count, current_min, current_max = active_treasury_cohort_fingerprint()
    if (
        current_sha256 != stored_rows_sha256
        or current_count != receipt.observation_count
        or current_min != receipt.reference_min
        or current_max != receipt.reference_max
    ):
        raise TreasuryReceiptReconciliationRequired(
            "matching Treasury commit intent does not match the active complete cohort"
        )
    return FetchResult(
        value={"receipt_replay": True, "receipt_observation_count": receipt.observation_count},
        network_called=False, received_count=0, valid_count=0, persisted_count=0,
        inserted_count=0, updated_count=0, reference_min=receipt.reference_min,
        reference_max=receipt.reference_max, source_data_date=receipt.reference_max,
        extracted_at=receipt.captured_at, request_count=0, retry_count=0,
    )


class TreasuryQuote(TypedDict):
    symbol: str
    name: str
    indexer: str
    maturityDate: str
    baseDate: str
    durationDays: int
    maturityLabel: str
    buyRate: float | None
    sellRate: float | None
    buyPrice: float | None
    sellPrice: float | None
    basePrice: float | None
    couponType: str


class YieldCurve(TypedDict):
    prefixado: list[TreasuryQuote]
    ipca: list[TreasuryQuote]
    error: NotRequired[str]


class TreasuryRefreshError(RuntimeError):
    row_count: int

    def __init__(self, row_count: int) -> None:
        self.row_count = row_count
        super().__init__("Tesouro Transparente returned no usable rows")


def fetch_treasury_quotes() -> list[TreasuryQuote]:
    try:
        rows = _load_latest_csv_rows()
        if not rows:
            return []

        today = datetime.now(timezone.utc).replace(tzinfo=None)
        max_base: datetime | None = None
        latest: dict[tuple[str, str], dict] = {}

        for row in rows:
            bond_type = row.get("Tipo Titulo", "").strip()
            maturity_str = row.get("Data Vencimento", "").strip()
            base_str = row.get("Data Base", "").strip()

            if not maturity_str or not base_str:
                continue

            try:
                base_date = _parse_date(base_str)
            except (IndexError, ValueError):
                continue

            if max_base is None or base_date > max_base:
                max_base = base_date

            key = (bond_type, maturity_str)
            prev = latest.get(key)
            if prev is not None and base_date <= prev["_base_date"]:
                continue

            latest[key] = {
                "_base_date": base_date,
                "_base_str": base_str,
                "bond_type": bond_type,
                "maturity_str": maturity_str,
                "buy_rate": row.get("Taxa Compra Manha", ""),
                "sell_rate": row.get("Taxa Venda Manha", ""),
                "buy_price": row.get("PU Compra Manha", ""),
                "sell_price": row.get("PU Venda Manha", ""),
                "base_price": row.get("PU Base Manha", ""),
            }

        result: list[TreasuryQuote] = []
        for b in latest.values():
            if b["_base_date"] != max_base:
                continue

            bond_type = b["bond_type"]

            if not bond_type.lower().startswith(ALLOWED_BOND_TYPES):
                continue

            maturity_str = b["maturity_str"]

            try:
                maturity_date = _parse_date(maturity_str)
                duration = (maturity_date - today).days
                duration = max(duration, 0)
            except (IndexError, ValueError):
                duration = 0

            if duration <= 0:
                continue

            maturity_label = _get_maturity_label(duration) if duration else "N/A"

            buy_rate = _parse_rate(b["buy_rate"])
            sell_rate = _parse_rate(b["sell_rate"])
            buy_price = _parse_rate(b["buy_price"])
            sell_price = _parse_rate(b["sell_price"])

            indexer = "prefixado"
            if "ipca" in bond_type.lower():
                indexer = "ipca"
            elif "selic" in bond_type.lower():
                indexer = "selic"

            symbol_base = bond_type.lower().replace(" ", "_").replace("+", "plus")
            symbol_maturity = maturity_str.replace("/", "-")

            result.append({
                "symbol": f"{symbol_base}_{symbol_maturity}",
                "name": bond_type,
                "indexer": indexer,
                "maturityDate": maturity_str,
                "baseDate": b["_base_str"],
                "durationDays": duration,
                "maturityLabel": maturity_label,
                "buyRate": buy_rate,
                "sellRate": sell_rate,
                "buyPrice": buy_price,
                "sellPrice": sell_price,
                "basePrice": _parse_rate(b["base_price"]),
                "couponType": "semestrais" if "juros" in bond_type.lower() else "zero",
            })

        return result

    except (IndexError, KeyError, TypeError, ValueError):
        return []


def fetch_yield_curve() -> YieldCurve:
    bonds = fetch_treasury_quotes()

    if not bonds:
        return {"error": "No data available", "prefixado": [], "ipca": []}

    prefixado = [
        b for b in bonds
        if b["indexer"] == "prefixado"
        and b["maturityDate"]
        and b["buyRate"] is not None
    ]
    ipca = [
        b for b in bonds
        if b["indexer"] == "ipca"
        and b["maturityDate"]
        and b["buyRate"] is not None
    ]

    prefixado.sort(key=lambda x: x["durationDays"])
    ipca.sort(key=lambda x: x["durationDays"])

    return {
        "prefixado": prefixado,
        "ipca": ipca,
    }


_CSV_CACHE: list[dict] | None = None
_LATEST_CSV_CACHE: list[dict] | None = None


def _load_csv_rows() -> list[dict] | None:
    global _CSV_CACHE
    if _CSV_CACHE is not None:
        return _CSV_CACHE
    rows = query_treasury_rows()
    if not rows:
        return None
    _CSV_CACHE = rows
    return rows


def _load_latest_csv_rows() -> list[dict] | None:
    global _LATEST_CSV_CACHE
    if _LATEST_CSV_CACHE is not None:
        return _LATEST_CSV_CACHE
    rows = query_treasury_latest_rows()
    if not rows:
        return None
    _LATEST_CSV_CACHE = rows
    return rows


def _download_csv_payload(*, deadline_at: float | None = None):
    budget = current_retry_budget()
    deadline = deadline_at or (budget.deadline_at if budget is not None else monotonic() + TREASURY_TOTAL_BUDGET_SECONDS)
    network_seconds = min(
        TREASURY_NETWORK_BUDGET_SECONDS,
        deadline - monotonic() - TREASURY_POST_NETWORK_RESERVE_SECONDS,
    )
    if network_seconds <= 0:
        raise TimeoutError("treasury cohort deadline leaves no bounded network budget")
    if budget is None:
        resp = get_provider_response_bounded(
            TESOURO_TRANSPARENTE_CSV, deadline_seconds=network_seconds, max_attempts=2,
            max_retry_wait_seconds=30, max_bytes=BINARY_MAX_BYTES,
            route_id="tesouro_daily_csv",
        )
    else:
        with acquisition_unit("tesouro:csv_cohort", 2, network_seconds):
            resp = get_provider_response_bounded(
                TESOURO_TRANSPARENTE_CSV, deadline_seconds=network_seconds, max_attempts=2,
                max_retry_wait_seconds=30, max_bytes=BINARY_MAX_BYTES,
                route_id="tesouro_daily_csv",
            )
    return resp.content, resp.headers.get("content-length")


def _download_csv_rows() -> list[dict]:
    payload, _content_length = _download_csv_payload()
    csv_text = payload.decode("utf-8-sig")
    return list(csv.DictReader(StringIO(csv_text), delimiter=";"))


def _refresh_treasury_data() -> FetchResult:
    global _CSV_CACHE, _LATEST_CSV_CACHE
    budget = current_retry_budget()
    intent = _v6_treasury_intent()
    if intent is not None:
        replay = _replay_v6_treasury_receipt(intent)
        if replay is not None:
            return replay
    operation_started = monotonic()
    configured_deadline = budget.deadline_at if budget is not None else operation_started + TREASURY_TOTAL_BUDGET_SECONDS
    root_deadline = min(configured_deadline, operation_started + TREASURY_TOTAL_BUDGET_SECONDS)
    operation_deadline = root_deadline - TREASURY_OUTCOME_RESERVE_SECONDS
    content, content_length = _download_csv_payload(deadline_at=operation_deadline)
    parsed, payload_hash = validate_treasury_csv(
        content,
        content_length=content_length,
        deadline_at=operation_deadline,
        capture_date=datetime.now(timezone.utc).date(),
    )
    base_dates = [item["base_date"] for item in parsed]
    captured_at = datetime.now(timezone.utc).isoformat()
    serialized = prepare_treasury_rows(parsed, deadline_at=operation_deadline)
    record_validated_network_receipt(
        provider="tesouro", route="primary", contract="tesouro-csv-cohort-v1",
        scope_id="tesouro:primary:primary", identity="daily-treasury-cohort",
        coverage=f"{min(base_dates)}..{max(base_dates)}", payload=content,
        captured_at=captured_at, validated_reference_count=len(parsed),
    )
    extraction_id = current_extraction_id() or str(uuid4())
    receipt = persist_treasury_rows(
        serialized,
        extraction_id=extraction_id,
        source_payload_sha256=payload_hash,
        captured_at=captured_at,
        reference_min=min(base_dates),
        reference_max=max(base_dates),
        deadline_at=operation_deadline,
        intent=intent,
    )
    _CSV_CACHE = None
    _LATEST_CSV_CACHE = None
    return FetchResult(
        value={"rows": receipt.observation_count},
        network_called=True, received_count=len(parsed), valid_count=len(parsed),
        persisted_count=receipt.observation_count, reference_min=receipt.reference_min,
        reference_max=receipt.reference_max, source_data_date=receipt.reference_max,
        extracted_at=receipt.captured_at,
    )


refresh_treasury_data = audited("tesouro", "bond_quotes", qualifiers=lambda p: {
    "dataset": "treasury_quotes_and_yields",
}, network_expected=True)(_refresh_treasury_data)


def fetch_bonds_history(codes: list[int], days: int = 90) -> list[dict]:
    rows = _load_csv_rows()
    return build_bonds_history(rows or [], codes, days)


def fetch_bond_catalog() -> list[dict]:
    rows = _load_csv_rows()
    return build_bond_catalog(rows or [])


def fetch_bond_history(code: int | str, days: int = 30) -> dict | None:
    rows = _load_csv_rows()
    return build_bond_history(rows or [], int(code), days)


def read_treasury_quotes() -> list[TreasuryQuote]:
    return fetch_treasury_quotes()


def read_yield_curve() -> YieldCurve:
    return fetch_yield_curve()


def read_bond_catalog() -> list[dict]:
    return fetch_bond_catalog()


def read_bond_history(code: int | str, days: int = 30) -> dict | None:
    return fetch_bond_history(code, days)


def read_bonds_history(codes: list[int], days: int = 90) -> list[dict]:
    return fetch_bonds_history(codes, days)
