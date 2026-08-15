import httpx
import json
from datetime import datetime, timedelta
from pathlib import Path
from config import BCB_ODATA_BASE, CACHE_DIR

CACHE_TTL = timedelta(hours=23)
FOCUS_CACHE = CACHE_DIR / "focus.json"


def _is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - mtime < CACHE_TTL


def fetch_focus(
    indicator: str,
    top_n: int = 100,
    use_cache: bool = True,
) -> list[dict]:
    if use_cache and _is_cache_fresh(FOCUS_CACHE):
        all_focus = json.loads(FOCUS_CACHE.read_text())
        if indicator in all_focus:
            return all_focus[indicator]

    url = f"{BCB_ODATA_BASE}/ExpectativasMercadoAnuais"
    params = {
        "$filter": f"Indicador eq '{indicator}'",
        "$orderby": "Data%20desc",
        "$top": str(top_n),
        "$format": "json",
    }

    resp = httpx.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json().get("value", [])

    all_focus = {}
    if _is_cache_fresh(FOCUS_CACHE):
        all_focus = json.loads(FOCUS_CACHE.read_text())
    all_focus[indicator] = data
    CACHE_DIR.mkdir(exist_ok=True)
    FOCUS_CACHE.write_text(json.dumps(all_focus, ensure_ascii=False))

    return data


def fetch_focus_monthly(
    indicator: str,
    top_n: int = 100,
    use_cache: bool = True,
) -> list[dict]:
    url = f"{BCB_ODATA_BASE}/ExpectativasMercadoMeses"
    params = {
        "$filter": f"Indicador eq '{indicator}'",
        "$orderby": "Data%20desc",
        "$top": str(top_n),
        "$format": "json",
    }

    resp = httpx.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def fetch_all_focus(use_cache: bool = True) -> dict:
    indicators = ["IPCA", "Selic", "PIB", "Câmbio", "IGP-M"]
    result = {}
    for ind in indicators:
        try:
            result[ind] = fetch_focus(ind, use_cache=use_cache)
        except Exception as e:
            result[ind] = {"error": str(e)}
    return result


def force_refresh_focus() -> dict:
    if FOCUS_CACHE.exists():
        FOCUS_CACHE.unlink()
    return fetch_all_focus(use_cache=False)
