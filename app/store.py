"""Saves each request (and everything the agent did for it) as one JSON file:
data/requests/REQ-0001.json. Simple, readable, easy to inspect by hand."""
import json
import threading

from . import config

DIR = config.DATA / "requests"
_lock = threading.Lock()


def new_id():
    with _lock:
        DIR.mkdir(parents=True, exist_ok=True)
        n = len(list(DIR.glob("REQ-*.json"))) + 1
        rid = f"REQ-{n:04d}"
        (DIR / f"{rid}.json").write_text("{}", encoding="utf-8")  # reserve the number
        return rid


def save(record):
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"{record['id']}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return record


def get(rid):
    f = DIR / f"{rid}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def all_records():
    if not DIR.exists():
        return []
    out = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(DIR.glob("REQ-*.json"))]
    return [r for r in out if r]
