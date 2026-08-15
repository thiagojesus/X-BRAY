import httpx
from datetime import datetime
from db.store import upsert_icva, query_icva, set_meta, get_meta

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


def _needs_refresh() -> bool:
    last = get_meta("icva_last_refresh")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return (datetime.now() - last_dt).total_seconds() > 23 * 3600
    except Exception:
        return True


def fetch_icva(use_cache: bool = True) -> dict:
    if use_cache and not _needs_refresh():
        cached = query_icva()
        if cached and cached.get("data"):
            return {
                "source": "Cielo Blog / ICVA",
                "sectors": SECTORS,
                "macro_sectors": MACRO_SECTORS,
                "data": cached["data"],
            }

    result = {
        "source": "Cielo Blog / ICVA",
        "sectors": SECTORS,
        "macro_sectors": MACRO_SECTORS,
        "note": "ICVA data scraped from Cielo blog releases. Historical XLS available at ri.cielo.com.br",
        "data": [],
    }

    try:
        for year in range(2024, 2027):
            for month in range(1, 13):
                blog_url = f"https://blog.cielo.com.br/indice-icva/vendas-varejo-janeiro-{year}/"
                try:
                    blog_resp = httpx.get(blog_url, timeout=15, follow_redirects=True, headers={
                        "User-Agent": "Mozilla/5.0"
                    })
                    if blog_resp.status_code == 200:
                        result["data"].append({
                            "year": year,
                            "month": month,
                            "status": "available",
                        })
                except Exception:
                    pass

        if result["data"]:
            upsert_icva(result["data"])
            set_meta("icva_last_refresh", datetime.now().isoformat())

    except Exception as e:
        result["error"] = str(e)

    return result


def force_refresh_icva() -> dict:
    set_meta("icva_last_refresh", "")
    return fetch_icva(use_cache=False)
