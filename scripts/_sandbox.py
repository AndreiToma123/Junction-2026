"""Import this FIRST in a test script. It points the app at a throw-away folder with a copy of the
DEFAULT rulebook, so tests never touch the real requests, orders, log, rule history or rules.yaml.
(Product data, usual orders and demo stock are only read, so they are shared.)"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIR = Path(tempfile.mkdtemp(prefix="buying-agent-test-"))
shutil.copy(ROOT / "rules.default.yaml", DIR / "rules.yaml")
os.environ["TTA_STATE_DIR"] = str(DIR)
os.environ["TTA_RULES_FILE"] = str(DIR / "rules.yaml")
atexit.register(shutil.rmtree, DIR, ignore_errors=True)
print(f"(test sandbox: {DIR}; your real data and rulebook are not touched)")
