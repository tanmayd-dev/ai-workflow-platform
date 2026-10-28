# API Testing Guide

All mutating endpoints require a JWT (`Authorization: Bearer <token>`) and, for
most of them, a role. Every sample below that mutates state shows the header
explicitly; read endpoints also need the token, so samples reuse it everywhere
except registration, login, and the health check.

## Server Startup

```bash
# From project root
source .venv/bin/activate
python -m app.main
```

Server will be available at: http://localhost:8000

API Documentation (Swagger UI): http://localhost:8000/docs

## Sample Endpoints

### 1. Health Check (no auth)

```bash
curl http://localhost:8000/health
```

### 2. Register and Login

Registration is public; login returns a bearer token. Save it once and reuse it
in every following request:

```bash
curl -X POST "http://localhost:8000/api/v1/auth/register" -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "password": "secret", "full_name": "You"}'

curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -d "username=you@example.com&password=secret"
# → {"access_token": "<token>", "token_type": "bearer"}
```

```bash
TOKEN="<paste access_token here>"
AUTH="Authorization: Bearer $TOKEN"
```

A freshly registered user has **no role** and gets 403 on every guarded
endpoint. Grant one with the assistant script (it imports the app, so `.env`
must be configured first):

```bash
python scripts/assign_role.py --email you@example.com --role author
```

To exercise the approval gate exactly as production would, prepare a second
user with the `approver` role instead of approving everything as `admin`:

```bash
curl -X POST "http://localhost:8000/api/v1/auth/register" -H "Content-Type: application/json" \
  -d '{"email": "approver@example.com", "password": "secret", "full_name": "Approver"}'

python scripts/assign_role.py --email approver@example.com --role approver

curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -d "username=approver@example.com&password=secret"
```

```bash
APPROVER_TOKEN="<paste access_token here>"
APPROVER_AUTH="Authorization: Bearer $APPROVER_TOKEN"
```

`admin` bypasses all role checks (seeded via `scripts/seed.py`,
admin@acmebank.com / admin123), but the walkthrough below uses dedicated roles.

### 3. Seed the Rule the Workflow Depends On (do this FIRST)

Workflow rule steps reference rules **by name** via
`config_json.rule_id`; at execution time the server resolves the rule's latest
active version. A run of a rule step without a matching rule fails, so seed the
rule before creating/executing the workflow. Requires the `author` role:

`POST /api/v1/rules` creates the rule **and** its active initial version (v1).
A simple high-risk threshold rule that the sample workflow below uses:

```bash
curl -X POST "http://localhost:8000/api/v1/rules" \
  -H "Content-Type: application/json" -H "$AUTH" \
  -d '{
    "name": "fraud_risk_threshold",
    "description": "Escalate transactions with a risk score above 0.8",
    "domain": "fraud",
    "logic_json": {
      "condition": {"field": "risk_score", "op": ">", "value": 0.8},
      "result": "ESCALATE"
    }
  }'
```

If the rule already exists the endpoint returns 409 — that is fine for a
seeding step. `GET /api/v1/rules` lists existing rules and versions.

Rule DSL in short: evaluate `condition` against the run's `input_data`
(`field`/`op` (`>`, `>=`, `<`, `<=`, `==`, `!=`, `and`/`or`/`not`)/`value`);
return `result` when the condition holds, and `null` otherwise. There is no
write-time validation of `logic_json`, so malformed logic surfaces only when a
run executes it.

### 4. Upload Document (Knowledge Base)

Requires the `author` role. Optional for this walkthrough (RAG context only
feeds AI steps); shown for completeness:

```bash
curl -X POST "http://localhost:8000/api/v1/documents/upload" \
  -H "$AUTH" \
  -F "file=@fraud_policy.txt" \
  -F "domain=fraud" \
  -F "description=Fraud detection policy document"
```

Uploaded documents are UTF-8 text, have no size limit yet, and cannot be
deleted via the API. `GET /api/v1/documents/{id}` fetches one. (Skip this step
if you want to stay fully offline — document ingestion needs the Chroma +
HuggingFace embedding model.)

### 5. Draft Workflow from Intent

Requires the `author` role — this actually calls OpenRouter, unlike every other
sample here:

```bash
curl -X POST "http://localhost:8000/api/v1/intents/draft" \
  -H "Content-Type: application/json" -H "$AUTH" \
  -d '{
    "intent": "Create a fraud assessment workflow for high-risk transactions",
    "domain": "fraud"
  }'
```

### 6. Create Workflow

Requires the `author` role. The example below is **offline-capable**: no
AI step, so no LLM is needed. It references the rule seeded in step 3 by name.
The `HUMAN_APPROVAL` step is what the run pauses on:

```bash
curl -X POST "http://localhost:8000/api/v1/workflows/draft" \
  -H "Content-Type: application/json" -H "$AUTH" \
  -d '{
    "name": "Fraud Assessment Workflow",
    "description": "Assess transaction fraud risk",
    "domain": "fraud",
    "steps": [
      {
        "order_index": 0,
        "step_type": "DOMAIN_RULE",
        "name": "Risk Threshold Check",
        "description": "Deterministic rule evaluates risk score",
        "config_json": {"rule_id": "fraud_risk_threshold"}
      },
      {
        "order_index": 1,
        "step_type": "HUMAN_APPROVAL",
        "name": "Manual Review",
        "description": "Human reviews flagged transactions",
        "config_json": {}
      }
    ]
  }'
```

To branch statically on input, add a `CONDITIONAL` step whose
`config_json.on_true`/`on_false` are **lists of step names** — the listed
branch bodies are wired exactly as given (required for multi-step bodies; a
string entry target still works for a single-step body). Example gate:

```json
{
  "order_index": 0,
  "step_type": "CONDITIONAL",
  "name": "Value Gate",
  "description": "Route by transaction size",
  "config_json": {
    "condition": {"field": "amount", "op": ">", "value": 10000},
    "on_true":  ["High-Risk Check"],
    "on_false": ["Standard Check"]
  }
}
```

Both branch targets must name later steps of the same workflow; unreachable or
ambiguous layouts are rejected with `ValueError` when the workflow is compiled
for a run.

### 7. Approve Workflow (Freezes It)

Requires the `approver` role (separate user/token in this walkthrough). Only
`APPROVED` workflows can be executed:

```bash
curl -X POST "http://localhost:8000/api/v1/workflows/{workflow_id}/approve" \
  -H "$APPROVER_AUTH"
```

### 8. Start Workflow Run

Requires the `executor` role. Give the designated user the `executor` role the
same way (`python scripts/assign_role.py --email you@example.com --role executor`)
or run this step as `admin`. `input_data` matters: domain rules and conditional
gates evaluate it, and it is stored for replay:

```bash
curl -X POST "http://localhost:8000/api/v1/runs/" \
  -H "Content-Type: application/json" -H "$AUTH" \
  -d '{
    "workflow_id": "{workflow_id}",
    "account_id": "ACC-12345",
    "input_data": {"transaction_id": "TX-1", "amount": 12000, "risk_score": 0.85},
    "trigger_reason": "High-value transaction detected"
  }'
```

The run queues before the response: you get **202 Accepted** with the run `id`
and `status: "RUNNING"`, and execution happens server-side after the response.
Poll `GET /api/v1/runs/{run_id}` (JWT required) until `status` reaches a
terminal state:

- `PAUSED` — the run hit a `HUMAN_APPROVAL` node; it is persisted in a real
  pending state and waits for step 9.
- `COMPLETED` / `FAILED` — execution finished; `output_data` holds the final
  decision output.

Only one active run per account exists at a time (a second concurrent start for
the same `account_id` returns 409).

### 9. Approve the Paused Run

Requires the `approver` role. `POST /api/v1/runs/{id}/approve` uses a
compare-and-swap on the persisted pending node: the approval atomically resumes
the graph, and the response reflects the resumed run:

```bash
curl -X POST "http://localhost:8000/api/v1/runs/{run_id}/approve" \
  -H "Content-Type: application/json" -H "$APPROVER_AUTH" \
  -d '{"notes": "Reviewed by fraud team"}'
```

`POST /api/v1/runs/{run_id}/reject` with `{"reason": "..."}` fails the run
instead. After approval, poll `GET /api/v1/runs/{run_id}` once more until
`COMPLETED`.

Note: maker-checker is not yet enforced — an approver may approve a run they
triggered themselves.

### 10. Run Result and Audit Trail

`GET /api/v1/runs/{run_id}` returns the run including `output_data` — the final
decision output of terminal runs: a **dict** for AI/approval steps (for an
approval step, e.g. the approver verdict and notes), a **scalar** for a rule
step (the rule's `result` value). It is `null` while the run is still
`RUNNING`/`PAUSED`.

```bash
curl "http://localhost:8000/api/v1/runs/{run_id}" -H "$AUTH"
```

`GET /api/v1/runs/{run_id}/executions` returns one row per node with its
`input_snapshot` and `output_snapshot` — the per-step audit trail, including
skipped branch steps:

```bash
curl "http://localhost:8000/api/v1/runs/{run_id}/executions" -H "$AUTH"
```

### 11. Replay Run

Requires the `executor` role. Re-executes a **COMPLETED** run with its stored
inputs. Replay is not deterministic: the LLM is called again and rules are
re-resolved from the latest active versions, not the versions the original run
used:

```bash
curl -X POST "http://localhost:8000/api/v1/runs/{run_id}/replay" \
  -H "$AUTH"
```

## Short Authoritative Walkthrough

`python scripts/demo.py` performs the exact flow above — login, rule seeding,
author/executor work as one user, workflow approval and run approval by a
separate `approver`-role user, polling to `PAUSED` and then `COMPLETED`, and a
final dump of executions and `output_data`. It needs no OpenRouter key.

## Sample Test Document

Create a file `fraud_policy.txt` with regulatory content:

```
Fraud Detection Policy

High-Risk Transaction Criteria:
1. Transaction amount exceeds $10,000
2. Transaction originates from sanctioned country
3. Multiple failed authentication attempts
4. Unusual transaction pattern for account

Required Actions:
- Risk score > 0.8: Immediate escalation to fraud team
- Risk score 0.5-0.8: Additional verification required
- Risk score < 0.5: Auto-approve with monitoring

All escalated transactions must be reviewed within 24 hours.
Manual approval required for any transaction over $50,000.
```

## Expected Flow

1. **Register/Login + Roles**: create author/executor and approver users via the API and `scripts/assign_role.py`
2. **Seed the Rule**: `POST /rules` for the logic the workflow's rule step references
3. **Upload Documents (optional)**: knowledge base for AI steps; needs the embedding model
4. **Create & Approve**: save the workflow, then freeze it via the approver
5. **Execute Run**: `POST /runs` returns 202; poll to `PAUSED`
6. **Human Approval**: the approver resumes the paused run; poll to `COMPLETED`
7. **Inspect Results**: `output_data` on the run + per-node executions
8. **Replay**: re-executes the completed run (LLM re-called, rules re-resolved — see caveat above)
