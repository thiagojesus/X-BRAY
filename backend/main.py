from datetime import datetime
from contextlib import asynccontextmanager
from threading import Thread

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

from db.store import init_db, db_stats, get_meta
from routes import juros, inflacao, ipca_decomposicao, atividade, cambio, titulos, focus, complementares, tesouro_direto, curvas_di
from datafetchers.bcb_sgs import fetch_sgs_batch
from datafetchers.anbima import fetch_anbima_ima
from datafetchers.focus import fetch_all_focus
from datafetchers.b3_di import fetch_di_curves
from config import INTEREST_RATES, INFLATION, ACTIVITY, EXCHANGE, COMPLEMENTARY


def daily_refresh(force: bool = False):
    print(f"[{datetime.now()}] Iniciando refresh diário...")
    try:
        fetch_sgs_batch(INTEREST_RATES, start_date="01/01/2015", use_cache=not force)
        fetch_sgs_batch(INFLATION, start_date="01/01/2015", use_cache=not force)
        fetch_sgs_batch(ACTIVITY, start_date="01/01/2015", use_cache=not force)
        fetch_sgs_batch(EXCHANGE, start_date="01/01/2015", use_cache=not force)
        fetch_sgs_batch(COMPLEMENTARY, start_date="01/01/2015", use_cache=not force)
        fetch_all_focus(use_cache=not force)
        fetch_anbima_ima(use_cache=not force)
        fetch_di_curves(days=30, use_cache=not force)
        print(f"[{datetime.now()}] Refresh diário concluído.")
    except Exception as e:
        print(f"[{datetime.now()}] Erro no refresh diário: {e}")


def _refresh_background(force: bool = False):
    Thread(target=daily_refresh, args=(force,), daemon=True).start()


scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(daily_refresh, "cron", hour=6, minute=0, id="daily_refresh")
    scheduler.start()
    _refresh_background()
    yield
    scheduler.shutdown()


app = FastAPI(
    title="X-BRAY API",
    description="Raio-X do Macro Brasileiro — API de indicadores econômicos",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
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
        "storage": "SQLite",
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
        "storage": "SQLite",
        "db_stats": db_stats(),
    }


@app.post("/api/refresh")
def refresh_all():
    _refresh_background(force=True)
    return {"status": "refresh started in background", "timestamp": datetime.now().isoformat()}
