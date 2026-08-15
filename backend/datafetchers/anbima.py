import httpx
import json
from io import BytesIO
from datetime import datetime, timedelta
from pathlib import Path
from python_calamine import CalamineWorkbook
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
        resp = httpx.get(
            ANBIMA_XLS_URL,
            timeout=120,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/vnd.ms-excel,*/*",
            },
        )
        resp.raise_for_status()

        xls_bytes = BytesIO(resp.content)
        wb = CalamineWorkbook.from_filelike(xls_bytes)

        result = {}
        for sheet_name in wb.sheet_names:
            clean_name = sheet_name.strip().lower().replace(" ", "_").replace("-", "_")
            sheet = wb.get_sheet_by_name(sheet_name)
            raw = sheet.to_python()
            if not raw:
                continue
            header = [str(h) if h else f"col_{i}" for i, h in enumerate(raw[0])]
            records = []
            for row in raw[1:]:
                rec = {}
                for i, h in enumerate(header):
                    val = row[i] if i < len(row) else None
                    if val == "" or val is None:
                        rec[h] = None
                    elif hasattr(val, "isoformat"):
                        rec[h] = val.isoformat()
                    else:
                        rec[h] = val
                records.append(rec)
            result[clean_name] = records[:500]

        CACHE_DIR.mkdir(exist_ok=True)
        ANBIMA_CACHE.write_text(json.dumps(result, ensure_ascii=False, default=str))
        return result

    except Exception as e:
        return {"error": str(e)}


def force_refresh_anbima() -> dict:
    if ANBIMA_CACHE.exists():
        ANBIMA_CACHE.unlink()
    return fetch_anbima_ima(use_cache=False)
