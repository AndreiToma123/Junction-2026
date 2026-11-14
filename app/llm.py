"""Connection to Google Gemini. The key is read from .env (GEMINI_API_KEY), never written in code.

If no key is set, available() is False and every AI job uses its plain-code fallback instead.
Gemini is only asked for JSON, at temperature 0, and its answer is always checked by code
before it is used (see ai.py). Gemini never decides whether an order is allowed.

Built to be frugal (the free tier allows only a few calls per minute and per day):
  - identical questions are answered from a small memory, without a new call
  - at most CALLS_PER_MINUTE calls; beyond that the plain-code fallback is used
  - after Gemini says "too many requests" we stop calling it for a while (cool-down)
Callers never wait or fail because of this: they just fall back to plain code.
"""
import hashlib
import json
import os
import time

import httpx

from . import config

MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CALLS_PER_MINUTE = int(os.getenv("GEMINI_CALLS_PER_MINUTE", "8"))
COOLDOWN_SECONDS = 90

_cache = {}         # question fingerprint -> answer
_recent = []        # times of recent calls
_state = {"cooldown_until": 0, "calls": 0, "from_memory": 0, "skipped": 0, "last_error": None}


class LLMError(Exception):
    pass


def available():
    return bool(config.GEMINI_API_KEY) and config.GEMINI_API_KEY != "put-your-key-here"


def usage():
    left = max(0, int(_state["cooldown_until"] - time.time()))
    return {"calls": _state["calls"], "answered_from_memory": _state["from_memory"], "skipped_to_save_quota": _state["skipped"],
            "cooling_down_seconds": left, "last_error": _state["last_error"], "calls_per_minute_limit": CALLS_PER_MINUTE}


def generate_json(system, user, timeout=20):
    """Ask Gemini for a JSON object. Raises LLMError on any problem (callers then fall back)."""
    if not available():
        raise LLMError("GEMINI_API_KEY is not set")
    key = hashlib.sha256((MODEL + "\x00" + system + "\x00" + user).encode()).hexdigest()
    if key in _cache:
        _state["from_memory"] += 1
        return json.loads(_cache[key])
    now = time.time()
    if now < _state["cooldown_until"]:
        _state["skipped"] += 1
        raise LLMError("Gemini quota reached, cooling down")
    _recent[:] = [t for t in _recent if now - t < 60]
    if len(_recent) >= CALLS_PER_MINUTE:
        _state["skipped"] += 1
        raise LLMError("own per-minute budget reached")
    _recent.append(now)
    _state["calls"] += 1
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "maxOutputTokens": 700},
    }
    try:
        r = httpx.post(URL.format(model=MODEL), json=body, timeout=timeout,
                       headers={"x-goog-api-key": config.GEMINI_API_KEY, "User-Agent": config.USER_AGENT})
    except httpx.HTTPError as e:
        _state["last_error"] = f"could not reach Gemini ({type(e).__name__})"
        raise LLMError(_state["last_error"])
    if r.status_code == 429:
        _state["cooldown_until"] = time.time() + COOLDOWN_SECONDS
        _state["last_error"] = "Gemini said: too many requests (free-tier limit)"
        raise LLMError(_state["last_error"])
    if r.status_code != 200:
        _state["last_error"] = f"Gemini returned HTTP {r.status_code}"
        raise LLMError(_state["last_error"])
    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
    except (KeyError, IndexError, ValueError, TypeError):
        _state["last_error"] = "Gemini's answer was not valid JSON"
        raise LLMError(_state["last_error"])
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        data = data[0]  # Gemini sometimes wraps the object in a list
    if not isinstance(data, dict):
        _state["last_error"] = "Gemini's answer was not a JSON object"
        raise LLMError(_state["last_error"])
    _state["last_error"] = None
    if len(_cache) > 300:
        _cache.clear()
    _cache[key] = json.dumps(data)
    return data
