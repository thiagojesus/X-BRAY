import httpx
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict

from db.store import set_meta, get_meta

GAMMA_EVENT_URL = "https://gamma-api.polymarket.com/events?slug=brazil-presidential-election-first-round-1st-place-in-{slug}"
NATIONAL_EVENT_URL = "https://gamma-api.polymarket.com/events?slug=brazil-presidential-election"
PRICES_URL = "https://clob.polymarket.com/prices-history?market={token}&interval=max&fidelity=1440"
REQUEST_TIMEOUT = 30
CACHE_TTL = 6 * 3600
CACHE_KEY = "polymarket_states"


class Candidate(TypedDict):
    name: str
    token: str
    price: float
    volume: float


class PublicCandidate(TypedDict):
    name: str
    price: float
    volume: float


class ElectionResult(TypedDict):
    candidates: list[PublicCandidate]
    history: dict[str, dict[str, float]]


class StatePoll(TypedDict):
    uf: str
    candidates: list[PublicCandidate]
    history: dict[str, dict[str, float]]


class StatePollPayload(TypedDict):
    source: str
    updated_at: str | None
    national: ElectionResult
    days: list[str]
    ufs: list[StatePoll]


class PolymarketRefreshError(RuntimeError):
    detail: str

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)

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


def _cache_get(allow_stale: bool = False) -> StatePollPayload | None:
    raw = get_meta(CACHE_KEY)
    ts = get_meta(f"{CACHE_KEY}_ts")
    if not raw or not ts:
        return None
    try:
        cached_at = datetime.fromisoformat(ts)
        if not allow_stale and (datetime.now() - cached_at).total_seconds() > CACHE_TTL:
            return None
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _cache_set(payload: StatePollPayload) -> None:
    set_meta(CACHE_KEY, json.dumps(payload, ensure_ascii=False, default=str))
    set_meta(f"{CACHE_KEY}_ts", datetime.now().isoformat())


def _is_valid_candidate(name: str) -> bool:
    if not name:
        return False
    if name in EXCLUDED_NAMES:
        return False
    return not any(name.startswith(p) for p in EXCLUDED_PREFIXES)


def _fetch_markets(url: str) -> list[dict]:
    resp = httpx.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data[0]["markets"] if isinstance(data, list) and data else []


def _fetch_event(slug: str) -> list[dict]:
    return _fetch_markets(GAMMA_EVENT_URL.format(slug=slug))


def _fetch_history(token: str) -> list[dict]:
    resp = httpx.get(PRICES_URL.format(token=token), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    return body.get("history", [])


def _to_date_key(t: int) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


def _candidate_name(market: dict) -> str:
    title = str(market.get("groupItemTitle") or "").strip()
    if title:
        return title
    question = str(market.get("question") or "").strip()
    prefix = "Will "
    suffix = " win the 2026 Brazilian presidential election"
    if question.startswith(prefix) and suffix in question:
        return question[len(prefix):question.index(suffix)]
    return question


def _extract_candidates(markets: list[dict], limit: int, rank_by_price: bool) -> list[Candidate]:
    candidates: list[Candidate] = []
    for m in markets:
        name = _candidate_name(m)
        if not _is_valid_candidate(name):
            continue
        price = float(m.get("lastTradePrice") or 0)
        volume = float(m.get("volume") or 0)
        if price < 0.003 and volume <= 100000:
            continue
        raw_tokens = m.get("clobTokenIds") or "[]"
        tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
        if not isinstance(tokens, list) or not tokens:
            continue
        candidates.append({"name": name, "token": str(tokens[0]), "price": price, "volume": volume})

    if rank_by_price:
        candidates.sort(key=lambda c: c["price"], reverse=True)
    else:
        candidates.sort(key=lambda c: c["price"] * c["volume"], reverse=True)
    return candidates[:limit]


def _fetch_candidate_history(candidates: list[Candidate]) -> dict[str, dict[str, float]]:
    history: dict[str, dict[str, float]] = {}
    for cand in candidates:
        pts = _fetch_history(cand["token"])
        for pt in pts:
            day = _to_date_key(int(pt["t"]))
            history.setdefault(day, {})[cand["name"]] = round(float(pt["p"]) * 100, 2)
    return history


def _public_candidates(candidates: list[Candidate]) -> list[PublicCandidate]:
    return [
        {
            "name": candidate["name"],
            "price": candidate["price"],
            "volume": candidate["volume"],
        }
        for candidate in candidates
    ]


def _fetch_national() -> ElectionResult:
    candidates = _extract_candidates(_fetch_markets(NATIONAL_EVENT_URL), limit=8, rank_by_price=True)
    history = _fetch_candidate_history(candidates)
    if not candidates or not history:
        raise PolymarketRefreshError("national election market returned no usable data")
    return {"candidates": _public_candidates(candidates), "history": history}


def _fetch_state(uf: str) -> StatePoll:
    candidates = _extract_candidates(_fetch_event(UF_SLUGS[uf]), limit=4, rank_by_price=False)
    history = _fetch_candidate_history(candidates)

    return {
        "uf": uf,
        "candidates": _public_candidates(candidates),
        "history": history,
    }


def fetch_state_polls(use_cache: bool = True, force: bool = False) -> StatePollPayload:
    if use_cache and not force:
        cached = _cache_get()
        if cached is not None:
            return cached

    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        national_future = pool.submit(_fetch_national)
        state_futures = {pool.submit(_fetch_state, uf): uf for uf in UF_SLUGS}
        states: list[StatePoll] = []
        for future, uf in state_futures.items():
            try:
                states.append(future.result())
            except (httpx.HTTPError, KeyError, TypeError, ValueError, RuntimeError, IndexError) as exc:
                errors.append(f"{uf}: {exc}")
        try:
            national = national_future.result()
        except (httpx.HTTPError, KeyError, TypeError, ValueError, RuntimeError, IndexError) as exc:
            errors.append(f"national: {exc}")

    if errors:
        raise PolymarketRefreshError(f"Polymarket refresh failed ({'; '.join(errors)})")

    states.sort(key=lambda s: s["uf"])

    all_days: set[str] = set()
    for s in states:
        all_days.update(s["history"].keys())
    days = sorted(all_days)

    payload: StatePollPayload = {
        "source": "Polymarket",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "national": national,
        "days": days,
        "ufs": states,
    }
    _cache_set(payload)
    return payload


def read_state_polls() -> StatePollPayload:
    cached = _cache_get(allow_stale=True)
    if cached is not None:
        return cached
    return {
        "source": "Polymarket",
        "updated_at": None,
        "national": {"candidates": [], "history": {}},
        "days": [],
        "ufs": [],
    }


def force_refresh_states() -> StatePollPayload:
    return fetch_state_polls(force=True)
