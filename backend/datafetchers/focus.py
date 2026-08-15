import httpx
from datetime import datetime
from config import BCB_ODATA_BASE
from db.store import upsert_focus, query_focus, set_meta, get_meta

INDICATORS = ["IPCA", "Selic", "PIB", "Câmbio", "IGP-M"]


def _needs_refresh() -> bool:
    last = get_meta("focus_last_refresh")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return (datetime.now() - last_dt).total_seconds() > 23 * 3600
    except Exception:
        return True


def fetch_focus(
    indicator: str,
    top_n: int = 200,
    use_cache: bool = True,
) -> list[dict]:
    if use_cache and not _needs_refresh():
        cached = query_focus(indicator)
        if cached and indicator in cached and cached[indicator]:
            return cached[indicator]

    url = f"{BCB_ODATA_BASE}/ExpectativasMercadoAnuais"
    params = {
        "$filter": f"Indicador eq '{indicator}'",
        "$orderby": "Data%20desc",
        "$top": str(top_n),
        "$format": "json",
    }

    try:
        resp = httpx.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json().get("value", [])
        if data:
            upsert_focus(indicator, data)
            set_meta("focus_last_refresh", datetime.now().isoformat())
    except Exception as e:
        print(f"[FOCUS] Erro indicator {indicator}: {e}")
        data = []

    cached = query_focus(indicator)
    if cached and indicator in cached:
        return cached[indicator]
    return data


def fetch_all_focus(use_cache: bool = True) -> dict:
    result = {}
    for ind in INDICATORS:
        try:
            result[ind] = fetch_focus(ind, use_cache=use_cache)
        except Exception as e:
            result[ind] = {"error": str(e)}
    return result


def force_refresh_focus() -> dict:
    set_meta("focus_last_refresh", "")
    return fetch_all_focus(use_cache=False)
