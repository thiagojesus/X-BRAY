import httpx
import json
from datetime import datetime, timedelta, date
from pathlib import Path
from config import BCB_SGS_BASE, CACHE_DIR

CACHE_TTL = timedelta(hours=23)
MAX_RANGE_YEARS = 10


def _cache_path(series_code: int) -> Path:
    return CACHE_DIR / f"sgs_{series_code}.json"


def _is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - mtime < CACHE_TTL


def _parse_date(d: str) -> date:
    parts = d.split("/")
    return date(int(parts[2]), int(parts[1]), int(parts[0]))


def _format_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _make_windows(start: str | None, end: str | None) -> list[tuple[str | None, str | None]]:
    if not start:
        return [(None, end)]

    s = _parse_date(start)
    e = _parse_date(end) if end else date.today()

    windows = []
    cursor = s
    while cursor < e:
        window_end = min(cursor.replace(year=cursor.year + MAX_RANGE_YEARS), e)
        windows.append((_format_date(cursor), _format_date(window_end)))
        cursor = window_end.replace(year=window_end.year + 1)

    return windows if windows else [(start, end)]


def fetch_sgs_series(
    code: int,
    start_date: str | None = None,
    end_date: str | None = None,
    use_cache: bool = True,
) -> list[dict]:
    cache = _cache_path(code)
    if use_cache and _is_cache_fresh(cache):
        return json.loads(cache.read_text())

    windows = _make_windows(start_date, end_date)
    all_data = []

    for w_start, w_end in windows:
        url = f"{BCB_SGS_BASE.format(code=code)}?formato=json"
        params = {}
        if w_start:
            params["dataInicial"] = w_start
        if w_end:
            params["dataFinal"] = w_end

        try:
            resp = httpx.get(url, params=params, timeout=90, follow_redirects=True)
            resp.raise_for_status()
            chunk = resp.json()
            all_data.extend(chunk)
        except Exception as e:
            print(f"[SGS] Erro serie {code} janela {w_start}-{w_end}: {e}")

    cache.write_text(json.dumps(all_data, ensure_ascii=False))
    return all_data


def fetch_sgs_last_n(code: int, n: int = 10) -> list[dict]:
    cache = _cache_path(code)
    if _is_cache_fresh(cache):
        all_data = json.loads(cache.read_text())
        return all_data[-n:]

    url = f"{BCB_SGS_BASE.format(code=code)}/ultimos/{n}?formato=json"
    resp = httpx.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data


def fetch_sgs_batch(codes: dict[str, int], start_date: str | None = None) -> dict[str, list[dict]]:
    result = {}
    for name, code in codes.items():
        try:
            result[name] = fetch_sgs_series(code, start_date=start_date)
        except Exception as e:
            result[name] = {"error": str(e)}
    return result


def force_refresh_sgs(code: int) -> list[dict]:
    cache = _cache_path(code)
    if cache.exists():
        cache.unlink()
    return fetch_sgs_series(code, use_cache=False)
