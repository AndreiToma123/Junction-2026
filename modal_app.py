"""Deploy the buying agent to Modal (https://modal.com) as a public web app.

    .venv/bin/modal deploy modal_app.py

What it does:
- builds a small Python image with requirements.txt and the app code from THIS folder
  (your keys, the 87 MB product file and your local demo data are NOT uploaded; the 9.5 MB
  search index data/catalog_index.json IS, so search works without the big file),
- keeps everything that changes (requests, orders, audit log, rules, kitchen, events) on a
  persistent Modal Volume "buying-agent-data", so it survives restarts and sleeps,
- runs ONE copy of the app so every tester sees the same data, and lets it sleep when idle,
- reads AIMO_API_KEY, GEMINI_API_KEY and DEMO_PASSWORD from the Modal Secret "buying-agent-keys".
"""
import pathlib

import modal

HERE = pathlib.Path(__file__).parent
REMOTE = "/root/app"
STATE = "/data/state"

# Never upload: keys, your local demo data, the big product file, the downloaded event pages.
IGNORE = [
    ".venv", ".venv/**", ".git", ".git/**", "**/__pycache__", "**/__pycache__/**", ".env", ".claude", ".claude/**",
    "data/requests", "data/requests/**", "data/events", "data/events/**", "data/rule_versions", "data/rule_versions/**",
    "data/log.jsonl", "data/orders.jsonl", "data/kitchen.json", "data/rule_changes.jsonl",
    "docs/aimo_api/valio_aimo_junction26_product_data.json", "docs/Challenge Instructions.txt",
    "Junction Platform.html", "Junction Platform_files", "Junction Platform_files/**",
    "Valio Aimo Junction 2026 Challenge - reference_files", "Valio Aimo Junction 2026 Challenge - reference_files/**",
    # private pitch material and the scripts that make it
    "voiceover", "voiceover/**", "slides", "slides/**", "PITCH.md", "PITCH_SCRIPT.md", "scripts/make_*.py",
]

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_requirements(str(HERE / "requirements.txt"))
    .env({"TTA_STATE_DIR": STATE, "TTA_RULES_FILE": f"{STATE}/rules.yaml"})
    .add_local_dir(str(HERE), remote_path=REMOTE, ignore=IGNORE)
)

volume = modal.Volume.from_name("buying-agent-data", create_if_missing=True)
app = modal.App("valio-buying-agent")


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("buying-agent-keys")],
    volumes={"/data": volume},
    max_containers=1,        # one copy: everyone shares the same requests, log and rules
    scaledown_window=900,    # sleep after 15 idle minutes (the first visit after that takes a few seconds)
    timeout=600,
)
@modal.concurrent(max_inputs=50)
@modal.asgi_app()
def web():
    import os
    import shutil
    import sys

    os.chdir(REMOTE)
    sys.path.insert(0, REMOTE)
    os.makedirs(STATE, exist_ok=True)
    if not os.path.exists(f"{STATE}/rules.yaml"):  # first start: the default rulebook
        shutil.copy(f"{REMOTE}/rules.default.yaml", f"{STATE}/rules.yaml")

    from app import log
    from app.main import app as fastapi_app

    if not os.path.exists(log.LOG_FILE):  # first start: an audit log with the example history
        from app import admin
        admin.reset_all("deployment", with_history=True)
        volume.commit()

    import threading
    import time

    last = {"t": 0.0}

    @fastapi_app.middleware("http")
    async def save_to_volume(request, call_next):
        """After anything that changes data, save the Volume (at most every 3 s) so nothing is lost on sleep."""
        response = await call_next(request)
        if request.method in ("POST", "PUT", "DELETE") and time.time() - last["t"] > 3:
            last["t"] = time.time()
            threading.Thread(target=volume.commit, daemon=True).start()
        return response

    return fastapi_app
