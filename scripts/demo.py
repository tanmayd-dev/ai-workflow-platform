"""
Authoritative authenticated end-to-end walkthrough of the workflow platform.

Everything happens against a live server (default http://127.0.0.1:8000) using
only the HTTP API. This script is offline-capable by design: the workflow uses
no AI_ANALYSIS step, so **no OpenRouter key and no LLM call is involved**.

What it does:
1. Log in as the seeded admin user (admin bypasses role checks, so it authors
   rules and the workflow and is allowed to start runs).
2. Create a dedicated approver user, grant the `approver` role with
   `scripts/assign_role.py`, and log in as that user.
3. Seed the DOMAIN_RULE logics the workflow references (via the rules API),
   skipping rules that already exist.
4. Create an offline-capable workflow: CONDITIONAL gate (explicit list branch
   bodies) + DOMAIN_RULE branch steps + shared HUMAN_APPROVAL join.
5. Approve (freeze) the workflow with the approver-role user.
6. Start a run (202 Accepted), poll to PAUSED, approve the paused run, poll to
   COMPLETED.
7. Print the per-node executions and the run's final `output_data`.

Prerequisites: a running server seeded with `python scripts/seed.py`, and
`.env` configured (the step-3 role assignment imports the app).

Credentials, overridable via environment:
- Admin (starts the run; `executor` role is bypassed by admin):
  DEMO_EMAIL / DEMO_PASSWORD, default admin@acmebank.com / admin123
- Approver (approves the workflow and the paused run):
  DEMO_APPROVER_EMAIL / DEMO_APPROVER_PASSWORD,
  default approver@acmebank.com / approver123
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE_URL = os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8000")
API = f"{BASE_URL}/api/v1"

ADMIN_EMAIL = os.environ.get("DEMO_EMAIL", "admin@acmebank.com")
ADMIN_PASSWORD = os.environ.get("DEMO_PASSWORD", "admin123")
APPROVER_EMAIL = os.environ.get("DEMO_APPROVER_EMAIL", "approver@acmebank.com")
APPROVER_PASSWORD = os.environ.get("DEMO_APPROVER_PASSWORD", "approver123")
POLL_INTERVAL_S = 0.5
POLL_TIMEOUT_S = 30

# DOMAIN_RULE steps reference rules by name; seed these via the rules API.
RULES = {
    "demo_high_risk_rule": {
        "name": "demo_high_risk_rule",
        "description": "High-risk transactions (risk_score > 0.8) are escalated",
        "domain": "demo",
        "logic_json": {
            "condition": {"field": "risk_score", "op": ">", "value": 0.8},
            "result": "ESCALATE_HIGH_RISK",
        },
    },
    "demo_high_value_rule": {
        "name": "demo_high_value_rule",
        "description": "High-value transactions get a secondary confirmation",
        "domain": "demo",
        "logic_json": {
            "condition": {"field": "amount", "op": ">", "value": 10000},
            "result": "HIGH_VALUE_CONFIRMED",
        },
    },
    "demo_standard_rule": {
        "name": "demo_standard_rule",
        "description": "Low-risk transactions proceed on the standard track",
        "domain": "demo",
        "logic_json": {
            "condition": {"field": "risk_score", "op": "<=", "value": 0.8},
            "result": "PROCEED_STANDARD",
        },
    },
}

STEPS = [
    {
        "order_index": 0,
        "step_type": "CONDITIONAL",
        "name": "Value Gate",
        "description": "Route by transaction size",
        "config_json": {
            "condition": {"field": "amount", "op": ">", "value": 10000},
            "on_true": ["High-Risk Check", "High-Value Secondary Check"],
            "on_false": ["Standard Check"],
        },
    },
    {
        "order_index": 1,
        "step_type": "DOMAIN_RULE",
        "name": "High-Risk Check",
        "description": "Rule evaluates the risk score",
        "config_json": {"rule_id": "demo_high_risk_rule"},
    },
    {
        "order_index": 2,
        "step_type": "DOMAIN_RULE",
        "name": "High-Value Secondary Check",
        "description": "Rule re-checks the transaction amount",
        "config_json": {"rule_id": "demo_high_value_rule"},
    },
    {
        "order_index": 3,
        "step_type": "DOMAIN_RULE",
        "name": "Standard Check",
        "description": "Rule evaluates the risk score",
        "config_json": {"rule_id": "demo_standard_rule"},
    },
    {
        "order_index": 4,
        "step_type": "HUMAN_APPROVAL",
        "name": "Manual Review",
        "description": "Human gate: the run pauses here until an approver resumes it",
        "config_json": {},
    },
]

INPUT_DATA = {"amount": 15000, "risk_score": 0.91}


def print_section(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def api(method: str, path: str, token: str | None = None, expect: int = 200,
        **kwargs) -> dict | list:
    """Perform an authenticated REST call and return the JSON payload.

    Attaches the bearer token when given and raises SystemExit on an
    unexpected status code, printing the response body for diagnosis.
    """
    headers = kwargs.pop("headers", {}) or {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.request(method, f"{API}{path}", headers=headers, timeout=30, **kwargs)
    if response.status_code != expect:
        raise SystemExit(
            f"✗ {method} {path} returned {response.status_code}, expected {expect}\n"
            f"  Body: {response.text}\n"
            "  Is the server running ('python -m app.main') and has seed.py been run?"
        )
    if not response.content:
        return {}
    return response.json()


def login(email: str, password: str) -> tuple[str, dict]:
    """Log in (OAuth2 form fields) and return (token, user profile)."""
    token_response = api(
        "POST",
        "/auth/login",
        expect=200,
        data={"username": email, "password": password},
    )
    me = api("GET", "/auth/me", token=token_response["access_token"], expect=200)
    return token_response["access_token"], me


def ensure_approver_user() -> tuple[str, dict]:
    """Register + assign the approver role to the demo approver, then log in."""
    register = requests.post(
        f"{API}/auth/register",
        json={"email": APPROVER_EMAIL, "password": APPROVER_PASSWORD, "full_name": "Demo Approver"},
        timeout=30,
    )
    if register.status_code == 201:
        print(f"  ✓ Registered approver user {APPROVER_EMAIL}")
    elif register.status_code in (400, 409):
        print(f"  = Approver user {APPROVER_EMAIL} already exists (register returned {register.status_code})")
    else:
        raise SystemExit(f"✗ Approver registration failed: {register.status_code} {register.text}")

    assign_role = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("assign_role.py")),
         "--email", APPROVER_EMAIL, "--role", "approver"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    output = (assign_role.stdout + assign_role.stderr).strip()
    print(f"  assign_role.py: {output}")
    if assign_role.returncode != 0:
        # assign_role.py writes to the app's database directly, so it only
        # works when that database is reachable from this shell (local dev
        # with a configured .env). Against a container the state lives in the
        # container's volume — grant the role from inside it instead.
        raise SystemExit(
            "✗ Could not grant the approver role from the host.\n"
            "  assign_role.py needs the app's .env and a locally reachable DB.\n"
            "  If the server runs in a container, grant the role inside it, e.g.:\n"
            "    podman exec <app_container> python scripts/assign_role.py \\\n"
            f"        --email {APPROVER_EMAIL} --role approver\n"
            "  (docker exec works the same way), then re-run this demo."
        )

    return login(APPROVER_EMAIL, APPROVER_PASSWORD)


def seed_rules(token: str) -> None:
    """Get-or-create every rule this workflow references, via the rules API."""
    existing = {rule["name"] for rule in api("GET", "/rules", token=token, expect=200)}
    for name, rule in RULES.items():
        if name in existing:
            print(f"  = Rule '{name}' already exists")
            continue
        api("POST", "/rules", token=token, expect=201, json=rule)
        print(f"  ✓ Created rule '{name}' (active version 1)")


def poll_run(run_id: uuid.UUID, token: str, while_status: str, until_status: str) -> dict:
    """Poll GET /runs/{id} until the status leaves `while_status`."""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    run = api("GET", f"/runs/{run_id}", token=token, expect=200)
    while run["status"] == while_status:
        if time.monotonic() > deadline:
            raise SystemExit(f"✗ Timed out waiting for the run to leave {while_status}")
        time.sleep(POLL_INTERVAL_S)
        run = api("GET", f"/runs/{run_id}", token=token, expect=200)
    print(f"  Run status: {run['status']}" + ("" if run["status"] == until_status else f" (expected {until_status})"))
    return run


def main() -> None:
    if urlparse(BASE_URL).scheme not in ("http", "https"):
        raise SystemExit(f"✗ Invalid DEMO_BASE_URL: {BASE_URL!r}")
    print_section("STEP 1 — Login as seeded admin (admin bypasses role checks)")
    print(f"  Defaults: DEMO_EMAIL={ADMIN_EMAIL!r} DEMO_PASSWORD set via env or "
          f"seed.py's admin@acmebank.com / admin123")
    admin_token, admin = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    print(f"  ✓ Logged in as {admin['email']} (id {admin['id']})")

    print_section("STEP 2 — Create approver user + grant approver role")
    approver_token, approver = ensure_approver_user()
    print(f"  ✓ Approver ready: {approver['email']} (id {approver['id']})")

    print_section("STEP 3 — Seed rules referenced by the workflow's rule steps")
    seed_rules(admin_token)

    print_section("STEP 4 — Create offline-capable workflow "
                  "(CONDITIONAL gate → DOMAIN_RULE branches → HUMAN_APPROVAL join)")
    workflow = api("POST", "/workflows/draft", token=admin_token, expect=201, json={
        "name": f"Demo Risk Workflow ({uuid.uuid4().hex[:8]})",
        "description": "demo.py: rule-gated branching with a human approval pause",
        "domain": "demo",
        "steps": STEPS,
    })
    workflow_id = workflow["id"]
    print(f"  ✓ Created workflow {workflow_id}")
    print(f"    status={workflow['status']} steps={[s['name'] for s in workflow['steps']]}")
    print(f"    created_by={workflow['created_by']} (equals the JWT caller id above)")

    print_section("STEP 5 — Approver freezes (approves) the workflow definition")
    workflow = api("POST", f"/workflows/{workflow_id}/approve", token=approver_token, expect=200)
    print(f"  ✓ Workflow status: {workflow['status']} approved_by={workflow['approved_by']}")

    print_section("STEP 6 — Start a run (202 Accepted, executed by a background task)")
    run = api("POST", "/runs/", token=admin_token, expect=202, json={
        "workflow_id": workflow_id,
        "account_id": f"ACC_DEMO_{uuid.uuid4().hex[:8]}",
        "input_data": INPUT_DATA,
        "trigger_reason": "demo.py walkthrough",
    })
    run_id = run["id"]
    print(f"  ✓ HTTP 202; run {run_id} status={run['status']}")
    print("  Polling until the run leaves RUNNING ...")
    run = poll_run(run_id, admin_token, while_status="RUNNING", until_status="PAUSED")
    if run["status"] != "PAUSED":
        raise SystemExit(f"✗ Run ended as {run['status']} instead of pausing at the "
                         f"approval step; error_message={run.get('error_message')}")
    print("  ✓ Run is PAUSED: a real persisted approval gate with a pending node row")

    print_section("STEP 7 — Approver approves the paused run (compare-and-swap resume)")
    run = api("POST", f"/runs/{run_id}/approve", token=approver_token, expect=200,
              json={"notes": "Approved by demo.py walkthrough"})
    print(f"  ✓ Approve accepted; status={run['status']}")
    run = poll_run(run_id, admin_token, while_status=run["status"], until_status="COMPLETED")
    if run["status"] != "COMPLETED":
        raise SystemExit(f"✗ Run finished as {run['status']}: "
                         f"error_message={run.get('error_message')}")

    print_section("STEP 8 — Node executions (per-node input/output snapshots)")
    for execution in api("GET", f"/runs/{run_id}/executions", token=admin_token, expect=200):
        print(f"  - {execution['node_type']:<14} {execution['status']:<9} "
              f"out={json.dumps(execution['output_snapshot'], default=str)}")

    print_section("STEP 9 — Final run output_data (terminal decision output)")
    print(json.dumps(run.get("output_data"), indent=2, default=str))
    print(f"\n✓ Walkthrough complete: run {run_id} COMPLETED")


if __name__ == "__main__":
    main()
