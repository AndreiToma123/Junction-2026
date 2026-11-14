"""Connection to Google Gemini. The key is read from .env (GEMINI_API_KEY), never written in code.

If no key is set, available() is False and every AI job uses its plain-code fallback instead.
Gemini is only asked for JSON, at temperature 0, and its answer is always checked by code
before it is used (see ai.py). Gemini never decides whether an order is allowed.
"""
import json
import os

import httpx

from . import config

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMError(Exception):
    pass


def available():
    return bool(config.GEMINI_API_KEY) and config.GEMINI_API_KEY != "put-your-key-here"


def generate_json(system, user, timeout=25):
    """Ask Gemini for a JSON object. Raises LLMError on any problem (callers then fall back)."""
    if not available():
        raise LLMError("GEMINI_API_KEY is not set")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    try:
        r = httpx.post(URL.format(model=MODEL), json=body, timeout=timeout,
                       headers={"x-goog-api-key": config.GEMINI_API_KEY, "User-Agent": config.USER_AGENT})
    except httpx.HTTPError as e:
        raise LLMError(f"Could not reach Gemini: {type(e).__name__}")
    if r.status_code != 200:
        raise LLMError(f"Gemini returned HTTP {r.status_code}")
    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
    except (KeyError, IndexError, ValueError, TypeError):
        raise LLMError("Gemini's answer was not valid JSON")
    if not isinstance(data, dict):
        raise LLMError("Gemini's answer was not a JSON object")
    return data
