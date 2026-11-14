"""Reset the demo to a clean start and write 3 weeks of GENERATED TEST HISTORY into the log.

Clears: requests, mock orders, audit log, rule-change history. Keeps: rules.yaml as it is.
The history lines are marked "test_history": true and their messages start with "[test history]".
They match data/usual_orders.json (24 Oct usual Valio, 31 Oct split with Arla, 7 Nov Valio UHT).

Run: .venv/bin/python -m scripts.reset_demo
"""
import shutil

from app import config, log, rule_edit, store

H = "[test history] "
HISTORY = [
    # 24 Oct: normal week
    ("2026-10-24T09:30:05+03:00", "REQ-H01", "agent", "request", "Read request: 1,000 L Lactose-free semi-skimmed milk, deadline Mon 26 Oct.", "simple pattern reader"),
    ("2026-10-24T09:30:07+03:00", "REQ-H01", "agent", "choose", "Chose O7: 1000 × 464306 (Valio), €1,150.00 incl. tax. Picked because it needs no approval, it uses the usual product, it is the cheapest usable option.", "agent"),
    ("2026-10-24T09:30:07+03:00", "REQ-H01", "agent", "order", "Placed mock order H-0001, €1,150.00 incl. tax, delivery Mon 26 Oct. Placed by agent.", "mock order service"),
    # 31 Oct: usual product short -> split with Arla, approved
    ("2026-10-31T09:41:10+02:00", "REQ-H02", "agent", "request", "Read request: 1,000 L Lactose-free semi-skimmed milk, deadline Mon 2 Nov.", "simple pattern reader"),
    ("2026-10-31T09:41:12+02:00", "REQ-H02", "agent", "split", "No single product can cover the full amount, so the agent built split options.", "agent"),
    ("2026-10-31T09:41:12+02:00", "REQ-H02", "agent", "choose", "Chose O15: 600 × 464306 (Valio) + 400 × 469363 (Arla), €1,814.00 incl. tax. It is the only usable option.", "agent"),
    ("2026-10-31T09:41:12+02:00", "REQ-H02", "agent", "rules", "C2 Approved brands: approve. Arla is not an approved brand (approved: Valio).", "rules.yaml"),
    ("2026-10-31T09:41:12+02:00", "REQ-H02", "agent", "approval", "Sent to the dashboard for a person to approve or reject.", "agent"),
    ("2026-10-31T10:05:40+02:00", "REQ-H02", "person:kitchen manager", "decision", "Approved by kitchen manager.", "dashboard"),
    ("2026-10-31T10:05:41+02:00", "REQ-H02", "agent", "order", "Placed mock order H-0002, €1,814.00 incl. tax, delivery Mon 2 Nov. Placed by human:kitchen manager.", "mock order service"),
    # 7 Nov: usual product out of stock -> same brand, different product (UHT)
    ("2026-11-07T09:20:02+02:00", "REQ-H03", "agent", "request", "Read request: 1,000 L Lactose-free semi-skimmed milk, deadline Mon 9 Nov.", "simple pattern reader"),
    ("2026-11-07T09:20:04+02:00", "REQ-H03", "agent", "lookup", "Usual product 464306 had 0 in stock for Mon 9 Nov.", "Aimo API"),
    ("2026-11-07T09:20:04+02:00", "REQ-H03", "agent", "choose", "Chose O9: 1000 × 464398 (Valio), €1,870.00 incl. tax. Official substitute: Valio Eila semi skimmed milk drink 1 l UHT lactose free. Picked because it needs no approval, it is the cheapest usable option.", "agent"),
    ("2026-11-07T09:20:05+02:00", "REQ-H03", "agent", "order", "Placed mock order H-0003, €1,870.00 incl. tax, delivery Mon 9 Nov. Placed by agent.", "mock order service"),
]

if __name__ == "__main__":
    shutil.rmtree(store.DIR, ignore_errors=True)
    config.ORDERS_FILE.unlink(missing_ok=True)
    log.LOG_FILE.unlink(missing_ok=True)
    rule_edit.CHANGES_FILE.unlink(missing_ok=True)
    shutil.rmtree(rule_edit.VERSIONS_DIR, ignore_errors=True)
    for time, rid, actor, step, msg, src in HISTORY:
        log.write(step, H + msg, src, actor=actor, request_id=rid, time=time, test_history=True)
    print(f"Demo reset. {len(HISTORY)} test-history lines written to {log.LOG_FILE.relative_to(config.ROOT)}.")
