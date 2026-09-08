"""
Enterprise App Builder — engine core (hackathon scaffold)

The whole bet of this track: an app is DATA the engine interprets,
not CODE the engine contains. This file proves that loop end-to-end.

Run:  python3 engine.py

What it demonstrates:
  1. A mock "system of record" (SQLite seeded to look like SAP + Workday).
  2. Connectors: the only layer that knows how to read/write a source.
  3. A meta-model: the tables that DESCRIBE an app (entities, rules,
     queue, actions) — all plain JSON-serializable data.
  4. An interpreter that runs ANY app definition against the connectors.
  5. Three apps built purely as definitions — including one the engine
     was never written for — to show breadth comes from config, not code.

There is NOT a single `if app == "finance"` anywhere in the engine.
"""

import sqlite3
import json
from typing import Any, Callable


# ----------------------------------------------------------------------
# 1. MOCK SYSTEM OF RECORD
#    In a real build this is SAP / Workday / a warehouse. Here it's SQLite
#    seeded with data, so the demo isn't blocked on enterprise auth.
# ----------------------------------------------------------------------
def build_mock_sor() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    # --- ERP-shaped data (pretend this is SAP) ---
    cur.execute("CREATE TABLE invoices (id TEXT, vendor TEXT, amount REAL, po_amount REAL, status TEXT)")
    cur.executemany("INSERT INTO invoices VALUES (?,?,?,?,?)", [
        ("INV-1001", "Acme Steel",   12400.00, 12400.00, "open"),
        ("INV-1002", "Acme Steel",   9800.00,  9500.00,  "open"),   # mismatch
        ("INV-1003", "Bolt Co",      450.00,   450.00,   "open"),
        ("INV-1004", "Bolt Co",      7300.00,  6100.00,  "open"),   # mismatch
    ])

    cur.execute("CREATE TABLE suppliers (id TEXT, name TEXT, risk_score REAL, on_time_rate REAL)")
    cur.executemany("INSERT INTO suppliers VALUES (?,?,?,?)", [
        ("SUP-1", "Acme Steel", 0.31, 0.97),
        ("SUP-2", "Bolt Co",    0.82, 0.61),   # high risk
        ("SUP-3", "Grid Parts", 0.74, 0.70),   # high risk
    ])

    # --- HRIS-shaped data (pretend this is Workday) ---
    cur.execute("CREATE TABLE onboarding (id TEXT, employee TEXT, laptop_provisioned INT, days_since_start INT)")
    cur.executemany("INSERT INTO onboarding VALUES (?,?,?,?)", [
        ("EMP-1", "Priya N",   1, 12),
        ("EMP-2", "Sam O",     0, 6),   # not provisioned, overdue
        ("EMP-3", "Lena K",    0, 1),
    ])

    db.commit()
    return db


# ----------------------------------------------------------------------
# 2. CONNECTOR
#    The ONLY component that knows a source's shape. Every app reuses it.
#    Read = fetch rows for an entity. Write = push an action's effect back.
# ----------------------------------------------------------------------
class SqlConnector:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def read(self, table: str) -> list[dict]:
        rows = self.db.execute(f"SELECT * FROM {table}").fetchall()
        return [dict(r) for r in rows]

    def write(self, table: str, key_field: str, key: str, updates: dict) -> None:
        sets = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [key]
        self.db.execute(f"UPDATE {table} SET {sets} WHERE {key_field}=?", vals)
        self.db.commit()


# ----------------------------------------------------------------------
# 3. THE ESCAPE HATCH
#    Config covers ~80% of apps. For the rest, a rule can name a custom
#    function registered here. The platform stays general by leaving a
#    clean seam instead of pretending config covers everything.
# ----------------------------------------------------------------------
CUSTOM_RULES: dict[str, Callable[[dict], bool]] = {}


def custom_rule(name: str):
    def deco(fn):
        CUSTOM_RULES[name] = fn
        return fn
    return deco


@custom_rule("supplier_composite_risk")
def _(record: dict) -> bool:
    # A fuzzier signal than a dropdown captures: blend score + delivery.
    return record["risk_score"] * 0.7 + (1 - record["on_time_rate"]) * 0.3 > 0.5


# ----------------------------------------------------------------------
# 4. RULE EVALUATION
#    Declarative conditions are a tiny JSON-serializable operator tree,
#    so an app definition can live as a row in a database. Operators here
#    are all the finance/HR/etc. detection logic most apps ever need.
# ----------------------------------------------------------------------
def evaluate(cond: dict, record: dict) -> bool:
    op = cond["op"]
    if op == "custom":      return CUSTOM_RULES[cond["fn"]](record)
    if op == "and":         return all(evaluate(c, record) for c in cond["all"])
    if op == "or":          return any(evaluate(c, record) for c in cond["all"])

    left = record[cond["field"]]
    if op == "neq_field":   return left != record[cond["other"]]
    if op == "eq_field":    return left == record[cond["other"]]
    if op == "gt":          return left > cond["value"]
    if op == "lt":          return left < cond["value"]
    if op == "eq":          return left == cond["value"]
    raise ValueError(f"unknown op: {op}")


# ----------------------------------------------------------------------
# 5. THE INTERPRETER  <-- this IS the engine
#    Give it any app definition; it produces a working queue and can run
#    the definition's actions. It knows nothing about finance, HR, etc.
# ----------------------------------------------------------------------
class Engine:
    def __init__(self, connector: SqlConnector):
        self.connector = connector
        self.audit: list[str] = []   # every action logged — cross-cutting

    def build_queue(self, app: dict) -> list[dict]:
        """Detection: read the entity, keep records the rule flags."""
        entity = app["entity"]
        records = self.connector.read(entity["table"])
        cases = [r for r in records if evaluate(app["rule"], r)]
        return cases

    def render(self, app: dict, cases: list[dict]) -> None:
        cols = app["queue"]["columns"]
        print(f"\n=== {app['name']} ===  ({len(cases)} item(s) in queue)")
        print("  " + " | ".join(c.ljust(14) for c in cols))
        for case in cases:
            print("  " + " | ".join(str(case[c]).ljust(14) for c in cols))
        print(f"  actions available: {[a['name'] for a in app['actions']]}")

    def run_action(self, app: dict, case_key: str, action_name: str) -> None:
        """A user takes a governed action; effect is written back + logged."""
        action = next(a for a in app["actions"] if a["name"] == action_name)
        entity = app["entity"]
        self.connector.write(entity["table"], entity["key"], case_key, action["writeback"])
        self.audit.append(f"{app['name']}: {action_name} on {case_key} -> {action['writeback']}")


# ----------------------------------------------------------------------
# 6. THREE APPS — as pure data. This is what a user (or the LLM) produces.
#    Note: adding an app = adding a definition. The engine is untouched.
# ----------------------------------------------------------------------
FINANCE_APP = {
    "name": "Finance — invoice/PO exceptions",
    "entity": {"table": "invoices", "key": "id"},
    "rule":   {"op": "neq_field", "field": "amount", "other": "po_amount"},
    "queue":  {"columns": ["id", "vendor", "amount", "po_amount"]},
    "actions": [
        {"name": "approve", "writeback": {"status": "approved"}},
        {"name": "flag",    "writeback": {"status": "flagged"}},
    ],
}

SUPPLIER_APP = {
    "name": "Supply chain — supplier risk",
    "entity": {"table": "suppliers", "key": "id"},
    "rule":   {"op": "custom", "fn": "supplier_composite_risk"},   # escape hatch
    "queue":  {"columns": ["id", "name", "risk_score", "on_time_rate"]},
    "actions": [
        {"name": "escalate", "writeback": {"risk_score": 1.0}},
    ],
}

# An app the engine was NEVER written for — invented to prove breadth.
# A team could hand this in live during judging.
HR_ONBOARDING_APP = {
    "name": "HR — overdue onboarding",
    "entity": {"table": "onboarding", "key": "id"},
    "rule":   {"op": "and", "all": [
        {"op": "eq", "field": "laptop_provisioned", "value": 0},
        {"op": "gt", "field": "days_since_start", "value": 3},
    ]},
    "queue":  {"columns": ["id", "employee", "days_since_start"]},
    "actions": [
        {"name": "provision", "writeback": {"laptop_provisioned": 1}},
    ],
}


# ----------------------------------------------------------------------
# 7. DEMO — same engine, three apps, one of them unplanned.
# ----------------------------------------------------------------------
if __name__ == "__main__":
    engine = Engine(SqlConnector(build_mock_sor()))

    for app in (FINANCE_APP, SUPPLIER_APP, HR_ONBOARDING_APP):
        cases = engine.build_queue(app)
        engine.render(app, cases)

    # Show a governed action + writeback + audit on the finance queue.
    print("\n--- taking an action ---")
    engine.run_action(FINANCE_APP, "INV-1002", "flag")
    engine.run_action(HR_ONBOARDING_APP, "EMP-2", "provision")

    print("\n--- audit log (cross-cutting, every app) ---")
    for line in engine.audit:
        print("  " + line)

    print("\n--- an app is just data (finance definition) ---")
    print(json.dumps(FINANCE_APP, indent=2))
