import json
from datetime import datetime, timedelta
from pathlib import Path
from config import CACHE_DIR

CACHE_TTL = timedelta(hours=23)
GLOBAL_CACHE = CACHE_DIR / "global.json"


def _is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - mtime < CACHE_TTL


def get_cached(key: str) -> dict | None:
    if not _is_cache_fresh(GLOBAL_CACHE):
        return None
    data = json.loads(GLOBAL_CACHE.read_text())
    return data.get(key)


def set_cached(key: str, value: dict):
    data = {}
    if GLOBAL_CACHE.exists():
        try:
            data = json.loads(GLOBAL_CACHE.read_text())
        except Exception:
            data = {}
    data[key] = value
    CACHE_DIR.mkdir(exist_ok=True)
    GLOBAL_CACHE.write_text(json.dumps(data, ensure_ascii=False, default=str))


def clear_cache():
    for f in CACHE_DIR.glob("*.json"):
        f.unlink()


def cache_info() -> dict:
    result = {}
    for f in CACHE_DIR.glob("*.json"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        age = datetime.now() - mtime
        result[f.stem] = {
            "age_hours": round(age.total_seconds() / 3600, 1),
            "fresh": age < CACHE_TTL,
            "size_kb": round(f.stat().st_size / 1024, 1),
        }
    return result
