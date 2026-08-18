"""Smoke test do caminho Postgres do store.py (dual-mode).

Roda contra um Postgres real via DATABASE_URL. Não faz parte do pytest —
é um teste de integração manual para validar o caminho Postgres.
"""
import os
import sys
import json

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/xbray_test",
)

sys.path.insert(0, os.path.dirname(__file__))
from db import store  # noqa: E402

FAILURES = []


def check(name: str, cond: bool, extra: str = ""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name}{(' — ' + extra) if extra else ''}")
    if not cond:
        FAILURES.append(name)


def main():
    assert store._is_pg(), "deveria estar em modo Postgres"
    print(f"[INFO] Modo: Postgres (DATABASE_URL set)")

    # 1. init_db
    store.init_db()
    check("init_db criou schema", True)
    for table in ["sgs", "anbima", "focus", "b3_di", "meta"]:
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
