"""Clearing and resetting, for demos and tests. Both need a person's name and are logged.

clear_requests: removes all requests and events (the list gets empty). Orders already placed, kitchen
                stock, rules and the audit log stay: the log is never edited, it gets a line saying so.
reset_all:      back to the very first start: default rules, default menu and kitchen stock, no requests,
                no orders, no events, no rule-change history, and a NEW audit log that starts with one line
                saying who reset it (optionally followed by the 3 weeks of example history).
"""
import shutil

from . import config, kitchen, log, rule_edit, rulebook, store


def clear_requests(by):
    from . import events
    n = len(list(store.DIR.glob("REQ-*.json"))) if store.DIR.exists() else 0
    m = len(list(events.DIR.glob("EV-*.json"))) if events.DIR.exists() else 0
    shutil.rmtree(store.DIR, ignore_errors=True)
    shutil.rmtree(events.DIR, ignore_errors=True)
    log.write("admin", f"All requests cleared by {by}: {n} requests and {m} events removed from the lists. "
              f"Orders already placed, kitchen stock, rules and this log are kept.", "dashboard", actor=f"person:{by}")
    return {"requests_removed": n, "events_removed": m}


def reset_all(by, with_history=False):
    from . import events
    shutil.rmtree(store.DIR, ignore_errors=True)
    shutil.rmtree(events.DIR, ignore_errors=True)
    config.ORDERS_FILE.unlink(missing_ok=True)
    rule_edit.CHANGES_FILE.unlink(missing_ok=True)
    shutil.rmtree(rule_edit.VERSIONS_DIR, ignore_errors=True)
    kitchen.STATE_FILE.unlink(missing_ok=True)
    shutil.copy(config.RULES_DEFAULT_FILE, config.RULES_FILE)
    rulebook.write()
    log.LOG_FILE.unlink(missing_ok=True)
    if with_history:
        from scripts.reset_demo import HISTORY, H
        for time, rid, actor, step, msg, src in HISTORY:
            log.write(step, H + msg, src, actor=actor, request_id=rid, time=time, test_history=True)
    log.write("admin", f"Everything reset to the defaults by {by}: rules, menu, kitchen stock, budgets; no requests, orders or events."
              + (" The 3 weeks of example history were added." if with_history else ""), "dashboard", actor=f"person:{by}")
    kitchen.load()  # recreate the default kitchen
    return {"ok": True, "with_history": with_history}
