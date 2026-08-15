import httpx
import pandas as pd
import json
from datetime import datetime, timedelta
from pathlib import Path
from config import ANBIMA_XLS_URL, CACHE_DIR

CACHE_TTL = timedelta(hours=23)
ANBIMA_CACHE = CACHE_DIR / "anbima_ima.json"


def _is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - mtime < CACHE_TTL


def fetch_anbima_ima(use_cache: bool = True) -> dict:
    if use_cache and _is_cache_fresh(ANBIMA_CACHE):
        return json.loads(ANBIMA_CACHE.read_text())

    try:
        resp = httpx.get(ANBIMA_XLS_URL, timeout=60, follow_redirects=True)
        resp.raise_for_status()

        xls_bytes = resp.content
        dfs = pd.read_excel(xls_bytes, sheet_name=None, engine="openpyxl")

        result = {}
        for sheet_name, df in dfs.items():
            clean_name = sheet_name.strip().lower().replace(" ", "_").replace("-", "_")
            records = df.to_dict(orient="records")
            for rec in records:
                for k, v in rec.items():
                    if pd.isna(v):
                        rec[k] = None
            result[clean_name] = records

        CACHE_DIR.mkdir(exist_ok=True)
        ANBIMA_CACHE.write_text(json.dumps(result, ensure_ascii=False, default=str))
        return result

    except Exception as e:
        return {"error": str(e)}


def force_refresh_anbima() -> dict:
    if ANBIMA_CACHE.exists():
        ANBIMA_CACHE.unlink()
    return fetch_anbima_ima(use_cache=False)
