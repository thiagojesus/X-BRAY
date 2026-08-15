import httpx
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from config import CACHE_DIR

CACHE_TTL = timedelta(hours=23)
CIELO_CACHE = CACHE_DIR / "icva.json"

SECTORS = [
    "Moda",
    "Móveis, Eletro e Departamento",
    "Vestuário",
    "Acessórios",
    "Pet Shop",
    "Farmácias e Perfumarias",
    "Supermercados e Hipermercados",
    "Postos de Combustíveis",
    "Alimentação - Padarias e Lanchonetes",
    "Livrarias e Papelarias",
    "Turismo e Transporte",
    "Alimentação - Bares e Restaurantes",
    "Hotelaria",
    "Saúde - Clínicas e Consultórios",
    "Educação",
    "Serviços",
    "E-commerce",
    "Outros",
]

MACRO_SECTORS = {
    "Bens Duráveis e Semiduráveis": [
        "Moda",
        "Móveis, Eletro e Departamento",
        "Vestuário",
        "Acessórios",
        "Pet Shop",
        "Farmácias e Perfumarias",
    ],
    "Bens Não Duráveis": [
        "Supermercados e Hipermercados",
        "Postos de Combustíveis",
        "Alimentação - Padarias e Lanchonetes",
        "Livrarias e Papelarias",
    ],
    "Serviços": [
        "Turismo e Transporte",
        "Alimentação - Bares e Restaurantes",
        "Hotelaria",
        "Saúde - Clínicas e Consultórios",
        "Educação",
        "Serviços",
        "E-commerce",
        "Outros",
    ],
}


def _is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - mtime < CACHE_TTL


def fetch_icva(use_cache: bool = True) -> dict:
    if use_cache and _is_cache_fresh(CIELO_CACHE):
        return json.loads(CIELO_CACHE.read_text())

    try:
        url = "https://www.cielo.com.br/inteligencia-de-dados/"
        resp = httpx.get(url, timeout=30, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        })

        result = {
            "source": "Cielo Blog / ICVA",
            "url": url,
            "sectors": SECTORS,
            "macro_sectors": MACRO_SECTORS,
            "note": "ICVA data scraped from Cielo blog releases. Historical XLS available at ri.cielo.com.br",
            "data": [],
        }

        for year in range(2024, 2027):
            blog_url = f"https://blog.cielo.com.br/indice-icva/vendas-varejo-janeiro-{year}/"
            try:
                blog_resp = httpx.get(blog_url, timeout=15, follow_redirects=True, headers={
                    "User-Agent": "Mozilla/5.0"
                })
                if blog_resp.status_code == 200:
                    result["data"].append({
                        "year": year,
                        "status": "available" if blog_resp.status_code == 200 else "unavailable",
                    })
            except Exception:
                pass

        CACHE_DIR.mkdir(exist_ok=True)
        CIELO_CACHE.write_text(json.dumps(result, ensure_ascii=False, default=str))
        return result

    except Exception as e:
        return {"error": str(e), "sectors": SECTORS, "macro_sectors": MACRO_SECTORS}


def force_refresh_icva() -> dict:
    if CIELO_CACHE.exists():
        CIELO_CACHE.unlink()
    return fetch_icva(use_cache=False)
