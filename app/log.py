"""The audit log: data/log.jsonl, one JSON object per line (JSON Lines, an open format).

One line = one step, in plain language, with where the fact came from:
  {"id": "L-000012", "time": "2026-11-14T11:05:04+02:00", "actor": "agent",
   "request_id": "REQ-0001", "step": "order", "message": "Placed mock order MOCK-0001 ...",
   "source": "mock order service", "data": {...}, "test_history": false,
   "prev_hash": "...", "hash": "..."}

actor: agent | person:<name> | buyer | system
Lines are only ever added, never changed. Each line carries the hash of the line before it
(a simple hash chain), so verify() can show that nobody edited or deleted a line afterwards.
"""
import hashlib
import json
import threading
from datetime import datetime

from . import config

LOG_FILE = config.DATA / "log.jsonl"
_lock = threading.Lock()
GENESIS = "0" * 64


def _hash(entry):
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read():
    if not LOG_FILE.exists():
        return []
    with open(LOG_FILE, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def write(step, message, source, actor="agent", request_id=None, data=None, time=None, test_history=False):
    with _lock:
        lines = read()
        entry = {
            "id": f"L-{len(lines) + 1:06d}",
            "time": time or datetime.now().astimezone().isoformat(timespec="seconds"),
            "actor": actor,
            "request_id": request_id,
            "step": step,
            "message": message,
            "source": source,
            "data": data or None,
            "test_history": test_history,
            "prev_hash": lines[-1]["hash"] if lines else GENESIS,
        }
        entry["hash"] = _hash(entry)
        config.DATA.mkdir(exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry


def verify():
    """Checks the hash chain. Returns {"ok": bool, "lines": n, "problem": text or None}."""
    prev = GENESIS
    lines = read()
    for e in lines:
        if e.get("prev_hash") != prev:
            return {"ok": False, "lines": len(lines), "problem": f"{e['id']}: the line before it was changed or removed."}
        if _hash(e) != e.get("hash"):
            return {"ok": False, "lines": len(lines), "problem": f"{e['id']}: this line was changed after it was written."}
        prev = e["hash"]
    return {"ok": True, "lines": len(lines), "problem": None}


def search(words=None, request_id=None, step=None, actor=None, since=None, until=None, product=None, limit=500):
    """Plain filter (no AI). All given filters must match. Newest first."""
    ws = [w.lower() for w in (words or "").split() if w.strip()]
    out = []
    for e in reversed(read()):
        if request_id and e.get("request_id") != request_id:
            continue
        if step and e["step"] != step:
            continue
        if actor and not e["actor"].startswith(actor):
            continue
        if since and e["time"][:10] < since:
            continue
        if until and e["time"][:10] > until:
            continue
        text = json.dumps(e, ensure_ascii=False).lower()
        if product and product.lower() not in text:
            continue
        if ws and not all(w in text for w in ws):
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out
