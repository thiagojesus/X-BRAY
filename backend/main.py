import os
from datetime import datetime
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db.store import init_db, db_stats, get_meta, DATABASE_URL

STORAGE_LABEL = "PostgreSQL" if DATABASE_URL else "SQLite"

DEFAULT_CORS = ["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"]
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()] or DEFAULT_CORS
from routes import juros, inflacao, ipca_decomposicao, atividade, cambio, titulos, focus, complementares, tesouro_direto, curvas_di
from datafetchers.bcb_sgs import fetch_sgs_batch
from datafetchers.anbima import fetch_anbima_ima
from datafetchers.focus import fetch_all_focus
from datafetchers.b3_di import fetch_di_curves
from config import INTEREST_RATES, INFLATION, ACTIVITY, EXCHANGE, COMPLEMENTARY


def daily_refresh(force: bool = False):
    print(f"[{datetime.now()}] Iniciando refresh diário...")
    tasks = [
        ("sgs_juros", lambda: fetch_sgs_batch(INTEREST_RATES, start_date="01/01/2015", use_cache=not force)),
        ("sgs_inflacao", lambda: fetch_sgs_batch(INFLATION, start_date="01/01/2015", use_cache=not force)),
        ("sgs_atividade", lambda: fetch_sgs_batch(ACTIVITY, start_date="01/01/2015", use_cache=not force)),
        ("sgs_cambio", lambda: fetch_sgs_batch(EXCHANGE, start_date="01/01/2015", use_cache=not force)),
        ("sgs_complementares", lambda: fetch_sgs_batch(COMPLEMENTARY, start_date="01/01/2015", use_cache=not force)),
        ("focus", lambda: fetch_all_focus(use_cache=not force)),
        ("anbima", lambda: fetch_anbima_ima(use_cache=not force)),
        ("b3_di", lambda: fetch_di_curves(days=30, use_cache=not force)),
    ]
    # Paralelo: refresh sequencial mede ~4min, acima do cap de 300s de serverless.
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks}
        for fut in futures:
            try:
                fut.result()
            except Exception as e:
                print(f"[{datetime.now()}] Erro em {futures[fut]}: {e}")
    print(f"[{datetime.now()}] Refresh diário concluído.")


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
    daily_refresh(force=force)
    return {"status": "refresh completed", "timestamp": datetime.now().isoformat()}
