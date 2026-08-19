import httpx
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from db.store import set_meta, get_meta

GAMMA_EVENT_URL = "https://gamma-api.polymarket.com/events?slug=brazil-presidential-election-first-round-1st-place-in-{slug}"
PRICES_URL = "https://clob.polymarket.com/prices-history?market={token}&interval=max&fidelity=1440"
REQUEST_TIMEOUT = 30
CACHE_TTL = 6 * 3600
CACHE_KEY = "polymarket_states"

UF_SLUGS = {
    "AC": "acre",
    "AL": "alagoas",
    "AM": "amazonas",
    "AP": "amapa",
    "BA": "bahia",
    "CE": "ceara",
    "DF": "federal-district",
    "ES": "espirito-santo",
    "GO": "goias",
    "MA": "maranhao",
    "MG": "minas-gerais",
    "MS": "mato-grosso-do-sul",
    "MT": "mato-grosso",
    "PA": "para",
    "PB": "paraiba",
    "PE": "pernambuco",
    "PI": "piaui",
    "PR": "parana",
    "RJ": "rio-de-janeiro",
    "RN": "rio-grande-do-norte",
    "RO": "rondonia",
    "RR": "roraima",
    "RS": "rio-grande-do-sul",
    "SC": "santa-catarina",
    "SE": "sergipe",
    "SP": "sao-paulo",
    "TO": "tocantins",
}

EXCLUDED_PREFIXES = ("Candidate ", "Person ")
EXCLUDED_NAMES = {"another person"}


def _cache_get() -> dict | None:
    raw = get_meta(CACHE_KEY)
    ts = get_meta(f"{CACHE_KEY}_ts")
    if not raw or not ts:
        return None
    try:
        cached_at = datetime.fromisoformat(ts)
        if (datetime.now() - cached_at).total_seconds() > CACHE_TTL:
            return None
        return json.loads(raw)
    except Exception:
        return None


def _cache_set(payload: dict):
    set_meta(CACHE_KEY, json.dumps(payload, ensure_ascii=False, default=str))
    set_meta(f"{CACHE_KEY}_ts", datetime.now().isoformat())


def _is_valid_candidate(name: str) -> bool:
    if not name:
        return False
    if name in EXCLUDED_NAMES:
        return False
    return not any(name.startswith(p) for p in EXCLUDED_PREFIXES)


def _fetch_event(slug: str) -> list[dict]:
    resp = httpx.get(GAMMA_EVENT_URL.format(slug=slug), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data[0]["markets"] if isinstance(data, list) and data else []


def _fetch_history(token: str) -> list[dict]:
    resp = httpx.get(PRICES_URL.format(token=token), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    return body.get("history", [])


def _to_date_key(t: int) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


def _fetch_state(uf: str) -> dict:
    markets = _fetch_event(UF_SLUGS[uf])
    candidates = []
    for m in markets:
        name = m.get("groupItemTitle") or ""
        if not _is_valid_candidate(name):
            continue
        price = float(m.get("lastTradePrice") or 0)
        volume = float(m.get("volume") or 0)
        if price < 0.003 and volume <= 100000:
            continue
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        if not tokens:
            continue
        candidates.append({"name": name, "token": tokens[0], "price": price, "volume": volume})

    candidates.sort(key=lambda c: c["price"] * c["volume"], reverse=True)
    candidates = candidates[:4]

    history: dict[str, dict[str, float]] = {}
    for cand in candidates:
        try:
            pts = _fetch_history(cand["token"])
        except Exception as e:
            print(f"[PM] {uf}/{cand['name']} history failed: {e}")
            continue
        for pt in pts:
            day = _to_date_key(int(pt["t"]))
            history.setdefault(day, {})[cand["name"]] = round(float(pt["p"]) * 100, 2)

    return {
        "uf": uf,
        "candidates": [{k: c[k] for k in ("name", "price", "volume")} for c in candidates],
        "history": history,
    }


def fetch_state_polls(use_cache: bool = True, force: bool = False) -> dict:
    if use_cache and not force:
        cached = _cache_get()
        if cached is not None:
            return cached

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_state, uf): uf for uf in UF_SLUGS}
        states = []
        for fut in futures:
            try:
                states.append(fut.result())
            except Exception as e:
                print(f"[PM] {futures[fut]} failed: {e}")

    states.sort(key=lambda s: s["uf"])

    all_days: set[str] = set()
    for s in states:
        all_days.update(s["history"].keys())
    days = sorted(all_days)

    payload = {
        "source": "Polymarket",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "ufs": states,
    }
    _cache_set(payload)
    return payload


def force_refresh_states() -> dict:
    return fetch_state_polls(force=True)