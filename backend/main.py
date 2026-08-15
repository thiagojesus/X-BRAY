import json
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

from routes import juros, inflacao, ipca_decomposicao, atividade, cambio, titulos, focus, icva, complementares
from cache.store import clear_cache, cache_info
from datafetchers.bcb_sgs import fetch_sgs_batch
from datafetchers.focus import fetch_all_focus
from config import INTEREST_RATES, INFLATION, ACTIVITY, EXCHANGE, COMPLEMENTARY


def daily_refresh():
    print(f"[{datetime.now()}] Iniciando refresh diário...")
    try:
        fetch_sgs_batch(INTEREST_RATES, start_date="01/01/2015")
        fetch_sgs_batch(INFLATION, start_date="01/01/2015")
        fetch_sgs_batch(ACTIVITY, start_date="01/01/2015")
        fetch_sgs_batch(EXCHANGE, start_date="01/01/2015")
        fetch_sgs_batch(COMPLEMENTARY, start_date="01/01/2015")
        fetch_all_focus()
        print(f"[{datetime.now()}] Refresh diário concluído.")
    except Exception as e:
        print(f"[{datetime.now()}] Erro no refresh diário: {e}")


scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(daily_refresh, "cron", hour=6, minute=0, id="daily_refresh")
    scheduler.start()
    daily_refresh()
    yield
    scheduler.shutdown()


app = FastAPI(
    title="X-BRAY API",
    description="Raio-X do Macro Brasileiro — API de indicadores econômicos",
    version="1.0.0",
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
app.include_router(icva.router)
app.include_router(complementares.router)


@app.get("/")
def root():
    return {
        "name": "X-BRAY API",
        "description": "Raio-X do Macro Brasileiro",
        "version": "1.0.0",
        "endpoints": {
            "juros": "/api/juros",
            "inflacao": "/api/inflacao",
            "ipca-decomposicao": "/api/ipca-decomposicao",
            "atividade": "/api/atividade",
            "cambio": "/api/cambio",
            "titulos": "/api/titulos",
            "focus": "/api/focus",
            "icva": "/api/icva",
            "complementares": "/api/complementares",
        },
    }


@app.get("/api/status")
def status():
    return {
        "status": "running",
        "timestamp": datetime.now().isoformat(),
        "cache": cache_info(),
    }


@app.post("/api/refresh")
def refresh_all():
    clear_cache()
    daily_refresh()
    return {"status": "all caches cleared and refreshed", "timestamp": datetime.now().isoformat()}
