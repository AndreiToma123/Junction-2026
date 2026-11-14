"""Paths and settings in one place. Keys are read from .env, never written in code."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

AIMO_BASE_URL = "https://junction26.valioaimo.fi"
AIMO_API_KEY = os.getenv("AIMO_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Our own honest client name, sent with every request to the Aimo API.
USER_AGENT = "junction26-buying-agent/0.1"
# Any text works here: the mock API ignores the customer number.
CUSTOMER_ID = "demo-kitchen"

DOCS = ROOT / "docs" / "aimo_api"
PRODUCT_FILE = DOCS / "valio_aimo_junction26_product_data.json"
RELIABILITY_FILE = DOCS / "valio_aimo_junction26_delivery_reliability.csv"
DATA = ROOT / "data"
CATALOG_INDEX = DATA / "catalog_index.json"   # small cache built from PRODUCT_FILE

# Things that change while the app runs (requests, orders, log, rule history) live in STATE.
# Normally that is ./data. Test scripts point it at a throw-away sandbox folder (see scripts/_sandbox.py)
# so that tests can never touch the real demo data or the real rulebook.
STATE = Path(os.getenv("TTA_STATE_DIR") or DATA)
RULES_FILE = Path(os.getenv("TTA_RULES_FILE") or ROOT / "rules.yaml")
RULES_DEFAULT_FILE = ROOT / "rules.default.yaml"
RULEBOOK_MD = RULES_FILE.parent / "rulebook.md"
ORDERS_FILE = STATE / "orders.jsonl"          # mock orders (ours, not Aimo's)
