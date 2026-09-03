import os
from datetime import datetime
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from db.store import init_db, db_stats, get_meta, set_meta, DATABASE_URL
from dataset_guard import DatasetUnavailableError

STORAGE_LABEL = "PostgreSQL" if DATABASE_URL else "SQLite"

DEFAULT_CORS = ["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"]
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()] or DEFAULT_CORS
from routes import juros, inflacao, ipca_decomposicao, atividade, cambio, titulos, focus, complementares, tesouro_direto, curvas_di, eleicoes
from datafetchers.bcb_sgs import fetch_sgs_batch
from datafetchers.anbima import fetch_anbima_ima
from datafetchers.focus import fetch_all_focus
from datafetchers.b3_di import fetch_di_curves
from datafetchers.polymarket import fetch_state_polls
from datafetchers.tesouro_direto import refresh_treasury_data
from config import (
    ACTIVITY,
    COMPLEMENTARY,
    EXCHANGE,
    INFLATION,
    INTEREST_RATES,
    IPCA_CORE,
    IPCA_GROUPS,
    IPCA_NATURE,
    IPCA_PRICES,
)


def daily_refresh(force: bool = False):
    print(f"[{datetime.now()}] Iniciando refresh diário...")
    tasks = [
        ("sgs_juros", lambda: fetch_sgs_batch(INTEREST_RATES, start_date="01/01/2015", use_cache=not force)),
        ("sgs_inflacao", lambda: fetch_sgs_batch(INFLATION, start_date="01/01/2015", use_cache=not force)),
        ("sgs_atividade", lambda: fetch_sgs_batch(ACTIVITY, start_date="01/01/2015", use_cache=not force)),
        ("sgs_cambio", lambda: fetch_sgs_batch(EXCHANGE, start_date="01/01/2015", use_cache=not force)),
        ("sgs_complementares", lambda: fetch_sgs_batch(COMPLEMENTARY, start_date="01/01/2015", use_cache=not force)),
        ("sgs_ipca_grupos", lambda: fetch_sgs_batch(IPCA_GROUPS, use_cache=not force)),
        ("sgs_ipca_naturezas", lambda: fetch_sgs_batch(IPCA_NATURE, use_cache=not force)),
        ("sgs_ipca_nucleos", lambda: fetch_sgs_batch(IPCA_CORE, use_cache=not force)),
        ("sgs_ipca_precos", lambda: fetch_sgs_batch(IPCA_PRICES, use_cache=not force)),
        ("focus", lambda: fetch_all_focus(use_cache=not force)),
        ("anbima", lambda: fetch_anbima_ima(use_cache=not force)),
        ("b3_di", lambda: fetch_di_curves(days=30, use_cache=not force)),
        ("polymarket", lambda: fetch_state_polls(use_cache=not force)),
        ("tesouro", refresh_treasury_data),
    ]
    results = []
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks}
        for fut in futures:
            try:
                payload = fut.result()
                if isinstance(payload, dict) and payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                results.append({"name": futures[fut], "status": "ok"})
            except Exception as e:  # noqa: BROAD_EXCEPT_OK - collect arbitrary provider failures without aborting remaining refreshes
                print(f"[{datetime.now()}] Erro em {futures[fut]}: {e}")
                results.append({"name": futures[fut], "status": "error", "error": str(e)})
    status = "ok" if all(result["status"] == "ok" for result in results) else "error"
    if status == "ok":
        set_meta("last_successful_update", datetime.now().isoformat())
    print(f"[{datetime.now()}] Refresh diário concluído.")
    return {"status": status, "tasks": results}


def _refresh_background(force: bool = False):
    daily_refresh(force=force)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="X-BRAY API",
    description="Raio-X do Macro Brasileiro — API de indicadores econômicos",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.exception_handler(DatasetUnavailableError)
async def dataset_unavailable_handler(_request: Request, exc: DatasetUnavailableError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": "dataset_unavailable", "dataset": exc.dataset},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(juros.router)
app.include_router(inflacao.router)
app.include_router(ipca_decomposicao.router)
app.include_router(atividade.router)
app.include_router(cambio.router)
app.include_router(titulos.router)
app.include_router(focus.router)
app.include_router(complementares.router)
app.include_router(tesouro_direto.router)
app.include_router(curvas_di.router)
app.include_router(eleicoes.router)


@app.get("/")
def root():
    return {
        "name": "X-BRAY API",
        "description": "Raio-X do Macro Brasileiro",
        "version": "2.0.0",
        "storage": STORAGE_LABEL,
        "endpoints": {
            "juros": "/api/juros",
            "inflacao": "/api/inflacao",
            "ipca-decomposicao": "/api/ipca-decomposicao",
            "atividade": "/api/atividade",
            "cambio": "/api/cambio",
            "titulos": "/api/titulos",
            "focus": "/api/focus",
            "complementares": "/api/complementares",
            "tesouro-direto": "/api/tesouro-direto",
            "curvas-di": "/api/curvas-di",
            "eleicoes-estados": "/api/eleicoes/estados",
        },
    }


@app.get("/api/status")
def status():
    return {
        "status": "running",
        "last_updated": get_meta("last_successful_update"),
        "timestamp": datetime.now().isoformat(),
        "storage": STORAGE_LABEL,
        "db_stats": db_stats(),
    }


@app.post("/api/refresh")
def refresh_all(force: bool = False):
    result = daily_refresh(force=force)
    return {**result, "timestamp": datetime.now().isoformat()}
