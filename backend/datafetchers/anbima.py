import httpx
from io import BytesIO
from datetime import datetime
from python_calamine import CalamineWorkbook
from config import ANBIMA_XLS_URL
from db.store import upsert_anbima, query_anbima, set_meta, get_meta


def _needs_refresh() -> bool:
    last = get_meta("anbima_last_refresh")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return (datetime.now() - last_dt).total_seconds() > 23 * 3600
    except Exception:
        return True


def fetch_anbima_ima(use_cache: bool = True) -> dict:
    if use_cache and not _needs_refresh():
        cached = query_anbima()
        if cached:
            return cached

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
            upsert_anbima(clean_name, records)

        set_meta("anbima_last_refresh", datetime.now().isoformat())
        return query_anbima()

    except Exception as e:
        return {"error": str(e)}


def force_refresh_anbima() -> dict:
    set_meta("anbima_last_refresh", "")
    return fetch_anbima_ima(use_cache=False)
