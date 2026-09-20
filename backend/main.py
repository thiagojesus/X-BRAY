import os
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from db.store import init_db, db_stats, get_meta, DATABASE_URL
from dataset_guard import DatasetUnavailableError
from domain_contracts import get_quality_snapshot
from refresh import (
    RefreshDependencies,
    RefreshInProgressError,
    daily_refresh as run_daily_refresh,
    refresh_status_payload,
)
from refresh_auth import require_refresh_auth

STORAGE_LABEL = "PostgreSQL" if DATABASE_URL else "SQLite"

DEFAULT_CORS = ["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"]
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()] or DEFAULT_CORS
from routes import juros, inflacao, ipca_decomposicao, atividade, cambio, titulos, focus, complementares, tesouro_direto, curvas_di, eleicoes, fiscal
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
    dependencies = RefreshDependencies(
        fetch_sgs_batch=fetch_sgs_batch,
        fetch_all_focus=fetch_all_focus,
        fetch_anbima_ima=fetch_anbima_ima,
        fetch_di_curves=fetch_di_curves,
        fetch_state_polls=fetch_state_polls,
        refresh_treasury_data=refresh_treasury_data,
    )
    return run_daily_refresh(force=force, dependencies=dependencies)


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


@app.exception_handler(RefreshInProgressError)
async def refresh_in_progress_handler(_request: Request, exc: RefreshInProgressError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"error": "refresh_in_progress"},
        headers={"Retry-After": str(exc.retry_after)},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials="*" not in CORS_ORIGINS,
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
app.include_router(fiscal.router)


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
            "fiscal": "/api/fiscal",
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


@app.get("/api/data-quality/{dataset}")
def data_quality(dataset: str):
    snapshot = get_quality_snapshot(dataset)
    if snapshot is not None:
        return snapshot
    return {
        "dataset": dataset,
        "status": "unavailable",
        "last_updated": None,
        "metadata": {},
        "unavailable": [],
    }


@app.get("/api/refresh-status")
def refresh_status():
    return refresh_status_payload()


@app.post("/api/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_all(force: bool = False):
    result = daily_refresh(force=force)
    return {**result, "timestamp": datetime.now().isoformat()}
