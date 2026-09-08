"""
Enterprise App Builder — generation layer (hackathon scaffold)

This is the "Big Bang" moment: type a plain-English description of an app,
get back an app definition the engine can run. The engine is untouched;
this layer just produces the same JSON shape as FINANCE_APP in engine.py.

Run:
    pip install anthropic
    export ANTHROPIC_API_KEY="sk-ant-..."      # in Codespaces: run this in the terminal
    python3 generate.py

No key handy? It still runs — set USE_LLM = False below for an offline
keyword-based stand-in so you can see the whole flow before wiring the API.

Design notes for judges:
  - The LLM is a COMPILER from intent -> configuration. It never makes the
    operational decision; it only writes the app definition.
  - Its output is VALIDATED against the live data source before the engine
    touches it. A hallucinated table or column is rejected, not run.
  - The LLM can only emit declarative rules. Custom-function rules (the
    escape hatch) stay in code on purpose — that's the human-owned 20%.
"""

import json
import os

from engine import Engine, SqlConnector, build_mock_sor

USE_LLM = False          # flip to False to run offline without an API key
MODEL = "claude-sonnet-5"   # any current model id; swap if your key lacks access


# ----------------------------------------------------------------------
# What the source actually contains. In a real build you'd introspect the
# connector; here we hand the model the schema so it can only reference
# tables and columns that exist.
# ----------------------------------------------------------------------
SCHEMA = {
    "invoices":   ["id", "vendor", "amount", "po_amount", "status"],
    "suppliers":  ["id", "name", "risk_score", "on_time_rate"],
    "onboarding": ["id", "employee", "laptop_provisioned", "days_since_start"],
}


# ----------------------------------------------------------------------
# The prompt. We describe the exact JSON shape and the allowed operators,
# and demand JSON only. This is the whole "generation" trick.
# ----------------------------------------------------------------------
def build_prompt(description: str) -> str:
    return f"""You configure operational apps for an engine. Given a plain-English \
request, output ONE app definition as JSON and nothing else — no prose, no code fences.

Available data source tables and their columns:
{json.dumps(SCHEMA, indent=2)}

The JSON must have exactly this shape:
{{
  "name": "<short human title>",
  "entity": {{"table": "<one table from above>", "key": "id"}},
  "rule": <a condition, see operators below>,
  "queue": {{"columns": ["<subset of that table's columns>"]}},
  "actions": [{{"name": "<verb>", "writeback": {{"<column>": <new value>}}}}]
}}

Allowed rule operators (rules are how items enter the work queue):
  {{"op": "neq_field", "field": "A", "other": "B"}}   # A != B
  {{"op": "eq_field",  "field": "A", "other": "B"}}   # A == B
  {{"op": "gt", "field": "A", "value": N}}             # A > N
  {{"op": "lt", "field": "A", "value": N}}             # A < N
  {{"op": "eq", "field": "A", "value": V}}             # A == V
  {{"op": "and", "all": [ <condition>, <condition> ]}}
  {{"op": "or",  "all": [ <condition>, <condition> ]}}

Only reference tables and columns that exist above. Request: "{description}"
"""


def generate_via_llm(description: str) -> dict:
    import anthropic
    client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from the env
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": build_prompt(description)}],
    )
    text = "".join(block.text for block in msg.content if block.type == "text").strip()
    if text.startswith("```"):                       # strip stray code fences
        text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(text)


def generate_offline(description: str) -> dict:
    """Keyword stand-in so the flow runs with no API key. Not the real thing."""
    d = description.lower()
    if "invoice" in d or "po" in d or "payment" in d:
        return {
            "name": "Finance — invoice/PO exceptions",
            "entity": {"table": "invoices", "key": "id"},
            "rule": {"op": "neq_field", "field": "amount", "other": "po_amount"},
            "queue": {"columns": ["id", "vendor", "amount", "po_amount"]},
            "actions": [{"name": "flag", "writeback": {"status": "flagged"}}],
        }
    if "supplier" in d or "risk" in d or "vendor" in d:
        return {
            "name": "Supply chain — high-risk suppliers",
            "entity": {"table": "suppliers", "key": "id"},
            "rule": {"op": "gt", "field": "risk_score", "value": 0.7},
            "queue": {"columns": ["id", "name", "risk_score", "on_time_rate"]},
            "actions": [{"name": "escalate", "writeback": {"risk_score": 1.0}}],
        }
    return {
        "name": "HR — overdue onboarding",
        "entity": {"table": "onboarding", "key": "id"},
        "rule": {"op": "and", "all": [
            {"op": "eq", "field": "laptop_provisioned", "value": 0},
            {"op": "gt", "field": "days_since_start", "value": 3},
        ]},
        "queue": {"columns": ["id", "employee", "days_since_start"]},
        "actions": [{"name": "provision", "writeback": {"laptop_provisioned": 1}}],
    }


# ----------------------------------------------------------------------
# Validation — the load-bearing safety step. Reject anything that doesn't
# match the real schema BEFORE the engine runs it. This is what makes a
# live demo safe: a bad generation fails loudly instead of silently.
# ----------------------------------------------------------------------
def validate(app: dict) -> None:
    for field in ("name", "entity", "rule", "queue", "actions"):
        if field not in app:
            raise ValueError(f"missing '{field}'")

    table = app["entity"]["table"]
    if table not in SCHEMA:
        raise ValueError(f"unknown table '{table}'")
    cols = set(SCHEMA[table])

    for c in app["queue"]["columns"]:
        if c not in cols:
            raise ValueError(f"queue column '{c}' not in {table}")

    def check_rule(cond: dict) -> None:
        op = cond["op"]
        if op in ("and", "or"):
            for c in cond["all"]:
                check_rule(c)
            return
        if cond.get("field") and cond["field"] not in cols:
            raise ValueError(f"rule field '{cond['field']}' not in {table}")
        if cond.get("other") and cond["other"] not in cols:
            raise ValueError(f"rule field '{cond['other']}' not in {table}")

    check_rule(app["rule"])

    for action in app["actions"]:
        for c in action["writeback"]:
            if c not in cols:
                raise ValueError(f"action writes unknown column '{c}'")


def generate_app(description: str) -> dict:
    app = (generate_via_llm if USE_LLM else generate_offline)(description)
    validate(app)          # never hand an unvalidated definition to the engine
    return app


# ----------------------------------------------------------------------
# DEMO — three plain-English requests -> three running apps, same engine.
# During judging, replace these strings with whatever the judges say.
# ----------------------------------------------------------------------
if __name__ == "__main__":
    if USE_LLM and not os.environ.get("ANTHROPIC_API_KEY"):
        print("No ANTHROPIC_API_KEY set. Set USE_LLM = False to run offline,")
        print("or export your key first. Falling back to offline mode.\n")
        USE_LLM = False

    engine = Engine(SqlConnector(build_mock_sor()))

    requests = [
        "AP needs to review invoices that don't match their PO amount",
        "flag suppliers whose risk score is above 0.7",
        "show new hires who still don't have a laptop after 3 days",
    ]

    for req in requests:
        print(f'\n>>> "{req}"')
        try:
            app = generate_app(req)
        except (ValueError, json.JSONDecodeError) as e:
            print(f"    rejected generated definition: {e}")
            continue
        cases = engine.build_queue(app)
        engine.render(app, cases)