import httpx
from datetime import date, datetime
from config import BCB_SGS_BASE
from db.store import upsert_sgs, query_sgs, query_sgs_latest, get_sgs_range, set_meta, get_meta

MAX_RANGE_YEARS = 10
REQUEST_TIMEOUT = 15

KNOWN_UNAVAILABLE = {21337, 18771}


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


def _needs_refresh(series_code: int) -> bool:
    key = f"sgs_last_refresh_{series_code}"
    last = get_meta(key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return (datetime.now() - last_dt).total_seconds() > 23 * 3600
    except Exception:
        return True


def fetch_sgs_series(
    code: int,
    start_date: str | None = None,
    end_date: str | None = None,
    use_cache: bool = True,
) -> list[dict]:
    if code in KNOWN_UNAVAILABLE:
        return query_sgs(code, start_date, end_date)

    db_min, db_max = get_sgs_range(code)

    if use_cache and not _needs_refresh(code) and db_min:
        return query_sgs(code, start_date, end_date)

    effective_start = start_date or "01/01/1990"

    windows = _make_windows(effective_start, end_date)
    all_data = []

    for w_start, w_end in windows:
        url = f"{BCB_SGS_BASE.format(code=code)}?formato=json"
        params = {}
        if w_start:
            params["dataInicial"] = w_start
        if w_end:
            params["dataFinal"] = w_end
        try:
            resp = httpx.get(url, params=params, timeout=REQUEST_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            chunk = resp.json()
            all_data.extend(chunk)
        except Exception as e:
            print(f"[SGS] Erro serie {code} janela {w_start}-{w_end}: {e}")

    if all_data:
        upsert_sgs(code, all_data)
        set_meta(f"sgs_last_refresh_{code}", datetime.now().isoformat())

    cached = query_sgs(code, start_date, end_date)
    if cached:
        return cached

    return all_data


def fetch_sgs_last_n(code: int, n: int = 10) -> list[dict]:
    if code in KNOWN_UNAVAILABLE:
        return query_sgs_latest(code, n)

    if not _needs_refresh(code):
        cached = query_sgs_latest(code, n)
        if cached:
            return cached

    url = f"{BCB_SGS_BASE.format(code=code)}/ultimos/{n}?formato=json"
    try:
        resp = httpx.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
        upsert_sgs(code, data)
        set_meta(f"sgs_last_refresh_{code}", datetime.now().isoformat())
    except Exception as e:
        print(f"[SGS] Erro last_n serie {code}: {e}")

    return query_sgs_latest(code, n)


def fetch_sgs_batch(codes: dict[str, int], start_date: str | None = None) -> dict[str, list[dict]]:
    result = {}
    for name, code in codes.items():
        try:
            result[name] = fetch_sgs_series(code, start_date=start_date)
        except Exception as e:
            result[name] = {"error": str(e)}
    return result


def force_refresh_sgs(code: int) -> list[dict]:
    key = f"sgs_last_refresh_{code}"
    set_meta(key, "")
    return fetch_sgs_series(code, use_cache=False)
