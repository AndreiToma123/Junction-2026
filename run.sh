#!/usr/bin/env bash
# Starts everything. First run installs what is needed (takes a minute).
# Then open http://localhost:8000 in a browser. Stop with Ctrl+C.
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "No .env file. Copy .env.example to .env and put your AIMO_API_KEY in it."
  exit 1
fi
if [ ! -x .venv/bin/uvicorn ]; then
  echo "First run: installing packages into .venv ..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if [ ! -f docs/aimo_api/valio_aimo_junction26_product_data.json ] && [ ! -f data/catalog_index.json ]; then
  echo "Product file missing. Download it from the Valio Aimo portal (/api/products/download)"
  echo "and save it as docs/aimo_api/valio_aimo_junction26_product_data.json"
  exit 1
fi
if [ ! -f data/log.jsonl ]; then
  .venv/bin/python -m scripts.reset_demo
fi
echo "Dashboard: http://localhost:8000    API docs: http://localhost:8000/docs"
exec .venv/bin/uvicorn app.main:app --port 8000
