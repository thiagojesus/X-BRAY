"""Smoke test do caminho Postgres do store.py (dual-mode).

Roda contra um Postgres real via DATABASE_URL. Não faz parte do pytest —
é um teste de integração manual para validar o caminho Postgres.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from pydantic import TypeAdapter

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/xbray_test",
)

sys.path.insert(0, os.path.dirname(__file__))
from db import store
from derived.inflation_target_inputs import (
    load_inflation_target_observations,
    observe_inflation_target_rules,
)
from derived.models import DerivedInput, DerivedResult, SnapshotOrigin
from derived.repository import insert_snapshot, query_snapshot_history
from macro_score.calculator import MacroScoreResult
from macro_score.inputs import score_cutoff
from macro_score.repository import (
    insert_macro_score_snapshot,
    query_latest_macro_score,
)
from macro_score.serialization import MacroScorePayload
from smoke_pg_macro_score import run_macro_score_smoke

FAILURES = []


def check(name: str, cond: bool, extra: str = ""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name}{(' — ' + extra) if extra else ''}")
    if not cond:
        FAILURES.append(name)


def main():
    assert store._is_pg(), "deveria estar em modo Postgres"
    print("[INFO] Modo: Postgres (DATABASE_URL set)")

    # 1. init_db
    store.init_db()
    check("init_db criou schema", True)
    for table in [
        "sgs",
        "anbima",
        "focus",
        "focus_forecasts",
        "b3_di",
        "meta",
        "sidra_observations",
        "observation_vintages",
        "derived_snapshots",
        "macro_score_snapshots",
    ]:
        store._execute(f"DELETE FROM {table}")

    # 2. sgs upsert + query
    store.upsert_sgs(4639, [
        {"data": "01/01/2024", "valor": "100,5"},
        {"data": "02/01/2024", "valor": "101,25"},
        {"data": "03/01/2024", "valor": "102,0"},
    ])
    # upsert repetido (conflito) deve atualizar sem duplicar
    store.upsert_sgs(4639, [{"data": "02/01/2024", "valor": "999,99"}])
    rows = store.query_sgs(4639)
    check("sgs upsert+query sem duplicar", len(rows) == 3, f"len={len(rows)}")
    check("sgs valor atualizado no conflito", rows[1]["valor"] == "999.99", rows[1]["valor"])
    check("sgs data convertida dd/mm/yyyy", rows[0]["data"] == "01/01/2024", rows[0]["data"])
    latest = store.query_sgs_latest(4639, 2)
    check("sgs query_sgs_latest", len(latest) == 2 and latest[-1]["valor"] == "102.0")
    rng = store.get_sgs_range(4639)
    check("sgs get_sgs_range", rng == ("2024-01-01", "2024-01-03"), str(rng))

    # 3. anbima upsert + query
    store.upsert_anbima("DI1", [
        {"Data de Referência": "2024-01-02", "Taxa": "10,5"},
        {"Data de Referência": "2024-01-03", "Taxa": "10,6"},
    ])
    anb = store.query_anbima("DI1")
    check("anbima upsert+query", len(anb.get("DI1", [])) == 2, str(list(anb.keys())))
    check("anbima json round-trip", anb["DI1"][0]["Taxa"] == "10,5")

    # 4. focus upsert + query
    store.upsert_focus("Selic", [
        {"Data": "2024-01-02T00:00:00", "Mediana": "9,5"},
        {"Data": "2024-01-09T00:00:00", "Mediana": "9,25"},
    ])
    foc = store.query_focus("Selic")
    check("focus upsert+query", len(foc.get("Selic", [])) == 2, str(list(foc.keys())))

    # 5. b3_di upsert + query (símbolos DI1 reais: maturidade embutida no ticker)
    store.upsert_b3_di([
        {"trade_date": "2024-01-02", "symbol": "DI1F25", "maturity": "2025-01-02", "rate": 10.5},
        {"trade_date": "2024-01-02", "symbol": "DI1G25", "maturity": "2025-02-03", "rate": 11.0},
    ])
    di = store.query_b3_di("2024-01-02", "2024-01-02")
    check("b3_di upsert+query", len(di) == 2, f"len={len(di)}")
    check("b3_di datas distintas", store.get_b3_di_dates() == ["2024-01-02"])

    # 6. meta
    store.set_meta("last_refresh", "2024-01-02T00:00:00")
    check("meta set/get", store.get_meta("last_refresh") == "2024-01-02T00:00:00")

    store.insert_sidra_observations([
        {
            "dataset_key": "pim_industria",
            "table_id": 8888,
            "variable_id": 12607,
            "classification_id": 544,
            "category_id": 129314,
            "territory_level": "N1",
            "territory_code": "1",
            "reference_period": "202607",
            "reference_label": "julho 2026",
            "value": 104.0,
            "unit": "index",
            "base_version": "2022=100",
            "source_published_at": None,
        }
    ])
    sidra = store.query_sidra_current(("pim_industria",))
    check("sidra append-only+query", sidra["pim_industria"][0]["value"] == 104.0)

    observed_rules = observe_inflation_target_rules()
    target_rules = load_inflation_target_observations(as_of=datetime.now(timezone.utc))
    check("inflation target ledger batch+query", observed_rules == 11 and len(target_rules) == 11)
    check(
        "inflation target availability uses persisted first-seen",
        all(rule.available_at == rule.first_seen_at for rule in target_rules),
    )

    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    derived_result = DerivedResult(
        key="smoke_ratio",
        value=5.0,
        unit="ratio",
        as_of=as_of,
        methodology_version="1.0.0",
        formula="series_a / 2",
        inputs=(DerivedInput("series_a", 10.0, "pct", "2026-08-01", as_of, "smoke"),),
        reason=None,
    )
    inserted = insert_snapshot(
        derived_result,
        calculation_cycle_id="postgres-smoke",
        origin=SnapshotOrigin.TEST_FIXTURE,
    )
    duplicate = insert_snapshot(
        derived_result,
        calculation_cycle_id="postgres-smoke-repeat",
        origin=SnapshotOrigin.TEST_FIXTURE,
    )
    check("derived snapshot append-only+idempotente", inserted and not duplicate)
    check("derived snapshot round-trip", query_snapshot_history("smoke_ratio")[0].result == derived_result)

    macro_calculated_at = datetime.now(timezone.utc)
    macro_as_of = macro_calculated_at - timedelta(seconds=1)
    macro_inputs_hash = sha256(b"macro-score-postgres-smoke").hexdigest()
    macro_result = MacroScoreResult(
        score=None,
        classification=None,
        calculated_at=macro_calculated_at,
        as_of=macro_as_of,
        score_month=score_cutoff(macro_as_of).score_month,
        methodology_version="0.1.0-beta",
        overall_coverage=0.0,
        valid_dimensions=0,
        total_dimensions=6,
        dimensions=(),
        components=(),
        input_fingerprint=macro_inputs_hash,
        warnings=("test_fixture",),
    )
    score_inserted = insert_macro_score_snapshot(
        macro_result,
        calculation_cycle_id="macro-score-postgres-smoke",
        origin=SnapshotOrigin.TEST_FIXTURE,
    )
    score_duplicate = insert_macro_score_snapshot(
        macro_result,
        calculation_cycle_id="macro-score-postgres-smoke-repeat",
        origin=SnapshotOrigin.TEST_FIXTURE,
    )
    score_rows = store.query_rows(
        "SELECT score_month, inputs_hash, payload_json FROM macro_score_snapshots"
    )
    score_payload = TypeAdapter(MacroScorePayload).validate_json(str(score_rows[0]["payload_json"]))
    check("macro score snapshot append-only+idempotente", score_inserted and not score_duplicate)
    check("macro score snapshot payload round-trip", len(score_rows) == 1 and score_payload["inputs_hash"] == macro_inputs_hash)
    check("macro score fixture origin remains non-production", query_latest_macro_score() is None)

    run_macro_score_smoke(check)

    # 7. db_stats
    stats = store.db_stats()
    check(
        "db_stats coerente",
        stats["sgs"] == 3 and stats["anbima"] == 2 and stats["focus"] == 2 and stats["b3_di"] == 2,
        json.dumps(stats),
    )

    print()
    if FAILURES:
        print(f"RESULTADO: {len(FAILURES)} falha(s): {FAILURES}")
        sys.exit(1)
    print("RESULTADO: todas as verificações passaram no caminho Postgres ✓")
    sys.exit(0)


if __name__ == "__main__":
    main()
