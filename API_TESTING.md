# API Testing Guide

## Server Startup

```bash
# From project root
source .venv/bin/activate
python -m app.main
```

Server will be available at: http://localhost:8000

API Documentation (Swagger UI): http://localhost:8000/docs

## Test Endpoints

### 1. Health Check
```bash
curl http://localhost:8000/health
```

### 1a. Authentication (required for runs)

`POST /api/v1/runs` requires a JWT. Register a user and login first:

```bash
curl -X POST "http://localhost:8000/api/v1/auth/register" -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "password": "secret", "full_name": "You"}'

curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -d "username=you@example.com&password=secret"
# → {"access_token": "...", "token_type": "bearer"}
```

Use `Authorization: Bearer <token>` on the run endpoints below.

### 2. Upload Document (Knowledge Base)
```bash
curl -X POST "http://localhost:8000/api/v1/documents/upload" \
  -H "Content-Type: multipart/form-data" \
  -F "file=@fraud_policy.txt" \
  -F "domain=fraud" \
  -F "description=Fraud detection policy document"
```

### 3. Draft Workflow from Intent
```bash
curl -X POST "http://localhost:8000/api/v1/intents/draft" \
  -H "Content-Type: application/json" \
  -d '{
    "intent": "Create a fraud assessment workflow for high-risk transactions",
    "domain": "fraud"
  }'
```

### 4. Create Workflow
```bash
curl -X POST "http://localhost:8000/api/v1/workflows/draft" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Fraud Assessment Workflow",
    "description": "Assess transaction fraud risk",
    "domain": "fraud",
    "steps": [
      {
        "order_index": 0,
        "step_type": "AI_ANALYSIS",
        "name": "Risk Scoring",
        "description": "AI analyzes transaction for fraud indicators",
        "config_json": {
          "rag_query": "fraud detection rules",
          "prompt_template": "Analyze this transaction for fraud risk"
        }
      },
      {
        "order_index": 1,
        "step_type": "DOMAIN_RULE",
        "name": "Risk Threshold Check",
        "description": "Deterministic rule evaluates risk score",
        "config_json": {
          "rule_id": "high_risk_threshold"
        }
      },
      {
        "order_index": 2,
        "step_type": "HUMAN_APPROVAL",
        "name": "Manual Review",
        "description": "Human reviews flagged transactions",
        "config_json": {}
      }
    ]
  }'
```

### 5. Approve Workflow
```bash
# Replace {workflow_id} with actual UUID from create response
curl -X POST "http://localhost:8000/api/v1/workflows/{workflow_id}/approve"
```

### 6. Start Workflow Run
```bash
curl -X POST "http://localhost:8000/api/v1/runs/" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{
    "workflow_id": "{workflow_id}",
    "account_id": "ACC-12345",
    "input_data": {"transaction_id": "TX-1", "amount": 12000, "risk_score": 0.85},
    "trigger_reason": "High-value transaction detected"
  }'
```

The run is queued: the response is **202 Accepted** with the run `id` and
`status: "RUNNING"`. Execution happens after the response in a background task.
Poll `GET /api/v1/runs/{run_id}` until `status` is `COMPLETED`, `FAILED`, or
`PAUSED` (waiting for human approval). `input_data` is required for meaningful
execution — domain rules are evaluated against it and it is stored for replay.

### 7. Get Run Audit Trail
```bash
curl "http://localhost:8000/api/v1/runs/{run_id}/executions"
```

### 8. Replay Run
```bash
curl -X POST "http://localhost:8000/api/v1/runs/{run_id}/replay" \
  -H "Authorization: Bearer <token>"
```

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

1. **Upload Documents**: Knowledge base with regulatory policies
2. **Draft Workflow**: AI generates workflow structure from intent
3. **Create & Approve**: Save draft, then approve to freeze it
4. **Execute Run**: Start execution for specific account
5. **View Audit Trail**: See all node executions with inputs/outputs
6. **Replay**: Verify determinism by re-executing
