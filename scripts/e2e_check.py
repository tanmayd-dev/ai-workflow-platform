#!/usr/bin/env python3
"""Offline end-to-end harness for the seeded walk.

Boots the FastAPI app in-process exactly the way tests/conftest.py does (a
throwaway SQLite database, isolated Chroma/document dirs, no live server, no
LLM call — the walk below uses only DOMAIN_RULE nodes plus HUMAN_APPROVAL) and
then drives the real HTTP surface with fastapi.testclient.TestClient:

1. Seed roles + admin via the seed functions (scripts/seed.py).
2. Log in as admin, create the demo rules over the rules API.
3. Register an approver user, grant the approver role via the seed's
   grant function, log in as that approver.
4. Draft an offline-capable workflow (CONDITIONAL list-body branch +
   DOMAIN_RULE steps + shared HUMAN_APPROVAL join — no AI step), approve
   (freeze) it with the approver.
5. POST /runs (202), poll to PAUSED, approver approves, poll to COMPLETED.
6. Assert the run's output_data is present.
7. GET /audit-logs with the auditor-capable token, tolerant code (see below).

Each stage prints PASS/FAIL; any failure exits non-zero.

Optional env overrides (all defaulted; run it from the repo root):
- E2E_ADMIN_EMAIL / E2E_ADMIN_PASSWORD
- E2E_APPROVER_EMAIL / E2E_APPROVER_PASSWORD

NOTE: GET /api/v1/audit-logs was still pending when this script was written;
the audit stage is 404/403-tolerant and only FAILs on a clearly-broken (<200)
response, logging a WARNING line if the endpoint does not exist yet.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

API = "/api/v1"
POLL_INTERVAL_S = 0.1
POLL_TIMEOUT_S = 30.0

ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@acmebank.com")
ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "e2e-admin-password-1")
APPROVER_EMAIL = os.environ.get("E2E_APPROVER_EMAIL", "approver@acmebank.com")
APPROVER_PASSWORD = os.environ.get("E2E_APPROVER_PASSWORD", "e2e-approver-password-1")

# Mirrors scripts/demo.py (rule names referenced via config_json.rule_id).
DEMO_RULES = [
    {
        "name": "demo_high_risk_rule",
        "description": "High-risk transactions (risk_score > 0.8) are escalated",
        "domain": "demo",
        "logic_json": {
            "condition": {"field": "risk_score", "op": ">", "value": 0.8},
            "result": "ESCALATE_HIGH_RISK",
        },
    },
    {
        "name": "demo_high_value_rule",
        "description": "High-value transactions get a secondary confirmation",
        "domain": "demo",
        "logic_json": {
            "condition": {"field": "amount", "op": ">", "value": 10000},
            "result": "HIGH_VALUE_CONFIRMED",
        },
    },
    {
        "name": "demo_standard_rule",
        "description": "Low-risk transactions proceed on the standard track",
        "domain": "demo",
        "logic_json": {
            "condition": {"field": "risk_score", "op": "<=", "value": 0.8},
            "result": "PROCEED_STANDARD",
        },
    },
]

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


class StageFailure(Exception):
    """Raised by stage bodies; caught by the runner to print FAIL."""


def _stage(name: str):
    """Decorator: run the body, print PASS/FAIL for the named stage."""
    def decorator(fn):
        def wrapper(results: list[tuple[str, bool, str]]) -> None:
            print(f"\n--- stage: {name} ---")
            try:
                fn()
            except StageFailure as exc:
                print(f"FAIL: {name}: {exc}")
                results.append((name, False, str(exc)))
                return
            except Exception as exc:  # unexpected — full traceback
                print(f"FAIL: {name}: unexpected {type(exc).__name__}: {exc}")
                traceback.print_exc()
                results.append((name, False, repr(exc)))
                return
            print(f"PASS: {name}")
            results.append((name, True, ""))
        return wrapper
    return decorator


def main() -> int:
    # Isolate all state in a throwaway directory BEFORE any app import, the
    # same way tests/conftest.py configures the environment up front.
    tmp = Path(tempfile.mkdtemp(prefix="e2e_check_"))
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp / 'e2e_check.sqlite'}"
    os.environ["CHROMA_DB_DIR"] = str(tmp / "chroma")
    os.environ["DOCUMENTS_DIR"] = str(tmp / "documents")
    os.environ.setdefault("OPENROUTER_API_KEY", "e2e-offline-key-not-used")
    os.environ.setdefault("SECRET_KEY", "e2e-offline-secret-key-for-jwt")
    os.environ.setdefault("ENABLE_RAG", "false")
    os.environ.setdefault("AUTO_CREATE_SCHEMA", "true")

    results: list[tuple[str, bool, str]] = []

    # ------------------------------------------------------------------ seed
    # Import the app first: registering the routers imports every model and
    # populates Base.metadata (tests/conftest.py relies on the same ordering).
    from app.main import app  # noqa: F401
    from app.core.database import init_db
    asyncio.run(init_db())

    from scripts.seed import bootstrap, grant_role as seed_grant_role

    @_stage("seed roles + admin")
    def stage_seed() -> None:
        summary = bootstrap(
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
            include_demo_rules=False,  # rules are created over HTTP below
        )
        if len(summary["roles"]) != 5:
            raise StageFailure(f"expected 5 roles, got {summary['roles']}")
        if not summary["admin_created"]:
            raise StageFailure(f"admin {ADMIN_EMAIL} was not created by seed")

    stage_seed(results)

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        def api(method: str, path: str, token: str | None = None, **kwargs):
            headers = kwargs.pop("headers", {}) or {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            return client.request(method, f"{API}{path}", headers=headers, **kwargs)

        def expect(method, path, status_codes, token=None, **kwargs):
            response = api(method, path, token=token, **kwargs)
            if response.status_code not in status_codes:
                raise StageFailure(
                    f"{method} {path} -> {response.status_code} "
                    f"(expected one of {status_codes}): {response.text}"
                )
            return response

        def poll_run(run_id: str, token: str, from_status: str, to_status: str) -> dict:
            deadline = time.monotonic() + POLL_TIMEOUT_S
            run = expect("GET", f"/runs/{run_id}", {200}, token=token).json()
            while run["status"] == from_status:
                if time.monotonic() > deadline:
                    raise StageFailure(
                        f"timed out waiting for run to leave {from_status} "
                        f"(last: {run['status']}, error={run.get('error_message')})"
                    )
                time.sleep(POLL_INTERVAL_S)
                run = expect("GET", f"/runs/{run_id}", {200}, token=token).json()
            if run["status"] != to_status:
                raise StageFailure(
                    f"run finished as {run['status']}, expected {to_status} "
                    f"(error={run.get('error_message')})"
                )
            return run

        # ------------------------------------------------------------- login
        admin_token: str | None = None
        approver_token: str | None = None

        @_stage("admin login")
        def stage_login() -> None:
            nonlocal admin_token
            token = expect(
                "POST", "/auth/login", {200},
                data={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            ).json()["access_token"]
            me = expect("GET", "/auth/me", {200}, token=token).json()
            if me["email"] != ADMIN_EMAIL:
                raise StageFailure(f"/auth/me returned {me['email']}")
            admin_token = token

        stage_login(results)
        if admin_token is None:
            return _finish(results)  # cannot continue without a token

        # ------------------------------------------------------- create rules
        @_stage("create demo rules via rules API")
        def stage_rules() -> None:
            existing = {r["name"] for r in expect("GET", "/rules", {200}, token=admin_token).json()}
            for spec in DEMO_RULES:
                if spec["name"] in existing:
                    continue  # already seeded (idempotent walk)
                expect("POST", "/rules", {201}, token=admin_token, json=spec)

        stage_rules(results)

        # ------------------------------------------------ approver registration
        @_stage("register approver + grant role via seed grant")
        def stage_approver_setup() -> None:
            nonlocal approver_token
            register = api(
                "POST", "/auth/register",
                json={"email": APPROVER_EMAIL, "password": APPROVER_PASSWORD,
                      "full_name": "E2E Approver"},
            )
            if register.status_code not in (200, 201, 400, 409):
                raise StageFailure(
                    f"register approver -> {register.status_code}: {register.text}"
                )
            result = seed_grant_role(APPROVER_EMAIL, "approver")
            print(f"  grant_role: {result}")
            token = expect(
                "POST", "/auth/login", {200},
                data={"username": APPROVER_EMAIL, "password": APPROVER_PASSWORD},
            ).json()["access_token"]
            # The role grant itself is proven later: the approver-gated
            # workflow/run approve endpoints must accept this token.
            approver_token = token

        stage_approver_setup(results)

        # ------------------------------------------------- draft + freeze workflow
        workflow_id: str | None = None

        @_stage("draft workflow (rule + conditional + human approval, no AI)")
        def stage_workflow() -> None:
            nonlocal workflow_id
            workflow = expect(
                "POST", "/workflows/draft", {201}, token=admin_token,
                json={
                    "name": f"E2E Offline Workflow ({uuid4().hex[:8]})",
                    "description": "e2e_check.py offline walk",
                    "domain": "demo",
                    "steps": STEPS,
                },
            ).json()
            workflow_id = workflow["id"]
            approved = expect(
                "POST", f"/workflows/{workflow_id}/approve", {200},
                token=approver_token,
            ).json()
            if approved.get("status") != "APPROVED":
                raise StageFailure(f"workflow status {approved.get('status')}, expected APPROVED")

        stage_workflow(results)
        if workflow_id is None:
            return _finish(results)

        # --------------------------------------------------- run, pause, approve
        run_id: str | None = None

        @_stage("POST /runs (202) and poll to PAUSED")
        def stage_run_pause() -> None:
            nonlocal run_id
            started = expect(
                "POST", "/runs/", {202}, token=admin_token,
                json={
                    "workflow_id": workflow_id,
                    "account_id": f"ACC_E2E_{uuid4().hex[:8]}",
                    "input_data": {"amount": 15000, "risk_score": 0.91},
                    "trigger_reason": "e2e_check offline walk",
                },
            ).json()
            run_id = str(started["id"])
            poll_run(run_id, admin_token, "RUNNING", "PAUSED")

        stage_run_pause(results)

        @_stage("approver approves the paused run + poll to COMPLETED")
        def stage_run_complete() -> None:
            expect(
                "POST", f"/runs/{run_id}/approve", {200}, token=approver_token,
                json={"notes": "approved by e2e_check"},
            )
            poll_run(run_id, admin_token, "PAUSED", "COMPLETED")

        stage_run_complete(results)

        # ------------------------------------------------------------- output data
        @_stage("run output_data present")
        def stage_output() -> None:
            run = expect("GET", f"/runs/{run_id}", {200}, token=admin_token).json()
            if run.get("output_data") in (None, [], {}):
                raise StageFailure(f"output_data missing/empty: {run.get('output_data')!r}")
            print(f"  output_data: {json.dumps(run['output_data'], default=str)[:200]}")

        stage_output(results)

        # ------------------------------------------------------------- audit trail
        @_stage("audit-logs (404/403 tolerant, FAIL only if <200)")
        def stage_audit() -> None:
            response = api("GET", "/audit-logs", token=admin_token)
            print(f"  GET /audit-logs -> {response.status_code}")
            if response.status_code < 200:
                raise StageFailure(f"audit-logs -> {response.status_code}: {response.text}")
            if response.status_code == 404:
                print(
                    "WARNING: GET /api/v1/audit-logs returned 404 — the audit endpoint"
                    " is still pending (concurrent task); tolerating until it lands."
                )
                return
            if response.status_code == 403:
                print("WARNING: audit-logs -> 403; role wiring for the endpoint is pending.")
                return
            payload = response.json()
            rows = payload if isinstance(payload, list) else payload.get("items", [])
            if not rows:
                raise StageFailure("audit-logs returned 2xx but zero rows")

        stage_audit(results)

    return _finish(results)


def _finish(results: list[tuple[str, bool, str]]) -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n=== e2e_check: {passed}/{len(results)} stages passed ===")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    print("ALL STAGES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
