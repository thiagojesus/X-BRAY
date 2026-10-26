import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from datafetchers.anbima import fetch_anbima_ima
from datafetchers.b3_di import fetch_di_curves
from datafetchers.bcb_sgs import fetch_sgs_batch
from datafetchers.focus import fetch_all_focus, fetch_focus_12m
from datafetchers.ibge_sidra import refresh_sidra_data
from datafetchers.tesouro_direto import refresh_treasury_data
from dataset_guard import DatasetUnavailableError
from db.store import DATABASE_URL, db_stats, get_meta, init_db
from derived.inflation_target_inputs import observe_inflation_target_rules
from derived.refresh import refresh_all_derived_snapshots
from domain_contracts import get_quality_snapshot
from macro_score.refresh import refresh_macro_score
from refresh import (
    RefreshDependencies,
    RefreshInProgressError,
    refresh_status_payload,
)
from refresh import daily_refresh as run_daily_refresh
from refresh_auth import require_refresh_auth
from routes import (
    atividade,
    atividade_sidra,
    cambio,
    complementares,
    credito,
    curvas_di,
    derivados,
    fiscal,
    focus,
    inflacao,
    ipca_decomposicao,
    juros,
    macro_score,
    refresh_runs,
    setor_externo,
    source_audit,
    source_candidates,
    tesouro_direto,
    titulos,
)

STORAGE_LABEL = "PostgreSQL" if DATABASE_URL else "SQLite"

DEFAULT_CORS = ["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"]
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()] or DEFAULT_CORS


def daily_refresh(force: bool = False):
    dependencies = RefreshDependencies(
        fetch_sgs_batch=fetch_sgs_batch,
        fetch_all_focus=fetch_all_focus,
        fetch_focus_12m=fetch_focus_12m,
        fetch_anbima_ima=fetch_anbima_ima,
        fetch_di_curves=fetch_di_curves,
        refresh_treasury_data=refresh_treasury_data,
        refresh_sidra_data=refresh_sidra_data,
        refresh_derived_snapshots=refresh_all_derived_snapshots,
        observe_inflation_target_rules=observe_inflation_target_rules,
        refresh_macro_score=refresh_macro_score,
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
app.include_router(atividade_sidra.router)
app.include_router(cambio.router)
app.include_router(titulos.router)
app.include_router(focus.router)
app.include_router(complementares.router)
app.include_router(tesouro_direto.router)
app.include_router(curvas_di.router)
app.include_router(fiscal.router)
app.include_router(credito.router)
app.include_router(setor_externo.router)
app.include_router(derivados.router)
app.include_router(macro_score.router)
app.include_router(refresh_runs.router)
app.include_router(source_audit.router)
app.include_router(source_candidates.router)


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
            "atividade-sidra": "/api/atividade/sidra",
            "cambio": "/api/cambio",
            "titulos": "/api/titulos",
            "focus": "/api/focus",
            "complementares": "/api/complementares",
            "tesouro-direto": "/api/tesouro-direto",
            "curvas-di": "/api/curvas-di",
            "fiscal": "/api/fiscal",
            "credito": "/api/credito",
            "setor-externo": "/api/setor-externo",
            "derivados": "/api/derivados",
            "macro-score": "/api/macro-score",
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
