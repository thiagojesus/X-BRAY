import httpx
import re
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

MONTH_PT = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril",
    5: "maio", 6: "junho", 7: "julho", 8: "agosto",
    9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro",
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


def _parse_icva_value(text: str) -> float | None:
    m = re.search(r"(\d+[.,]\d+)\s*%", text)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


def _scrape_blog_post(year: int, month: int) -> dict | None:
    month_name = MONTH_PT[month]
    slug = f"vendas-varejo-{month_name}-{year}"
    url = f"https://blog.cielo.com.br/indice-icva/{slug}/"
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        html = resp.text
        nominal = None
        real = None
        m_nom = re.search(r"cresceu\s+([\d,]+)%", html, re.IGNORECASE)
        if m_nom:
            nominal = float(m_nom.group(1).replace(",", "."))
        m_real = re.search(r"real.*?([\d,]+)%", html, re.IGNORECASE)
        if m_real:
            real = float(m_real.group(1).replace(",", "."))
        m_nom2 = re.search(r"alta de\s+([\d,]+)%", html, re.IGNORECASE)
        if m_nom2 and nominal is None:
            nominal = float(m_nom2.group(1).replace(",", "."))
        m_nom3 = re.search(r"queda de\s+([\d,]+)%", html, re.IGNORECASE)
        if m_nom3 and nominal is None:
            nominal = -float(m_nom3.group(1).replace(",", "."))
        m_real2 = re.search(r"recuo.*?([\d,]+)%", html, re.IGNORECASE)
        if m_real2 and real is None:
            real = -float(m_real2.group(1).replace(",", "."))
        if nominal is not None:
            return {"year": year, "month": month, "nominal": nominal, "real": real}
    except Exception:
        pass
    return None


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
        "data": [],
    }

    records = []
    for year in [2024, 2025, 2026]:
        end_month = 12 if year < 2026 else datetime.now().month
        for month in range(1, end_month + 1):
            parsed = _scrape_blog_post(year, month)
            if parsed:
                records.append({
                    "year": year,
                    "month": month,
                    "nominal": parsed["nominal"],
                    "real": parsed["real"],
                    "data": f"{year}-{str(month).zfill(2)}-01",
                })

    if records:
        upsert_icva(records)
        set_meta("icva_last_refresh", datetime.now().isoformat())
        result["data"] = records
    else:
        result["data"] = []

    return result


def force_refresh_icva() -> dict:
    set_meta("icva_last_refresh", "")
    return fetch_icva(use_cache=False)
