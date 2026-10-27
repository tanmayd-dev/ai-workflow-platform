# AI-Driven Enterprise Decision Workflow Platform

A prototype of an AI-governed workflow orchestration system for banking operations:
AI drafts workflows from natural-language intents, deterministic rules bound the
decisions, and every step is recorded for audit and replay.

**This is a work-in-progress prototype, not a deployed system.** The status table
below is the single source of truth for what works; this project previously
carried self-congratulatory status docs (`PROJECT_COMPLETE.md`, `SYSTEM_COMPLETE.md`,
etc.) whose claims contradicted each other — they were removed and replaced by
this table.

## Verified state

| Area | Status | Notes |
|---|---|---|
| Auth (register/login/me) | ✅ Works | SHA-256+salt password hashing, JWT bearer tokens (see note below) |
| RBAC (roles on endpoints) | ✅ Enforced | `admin`/`author`/`approver`/`executor`/`auditor`; strict mapping (no role → 403), `admin` bypasses |
| Document upload + RAG ingestion | ✅ Works | ChromaDB + HuggingFace embeddings, per-domain collections |
| Workflow CRUD (draft/approve/freeze) | ✅ Works | `created_by` is still a placeholder UUID, not the caller |
| Workflow | ✅ Works | Compiles to a real LangGraph `StateGraph` (AI / rule / approval / conditional nodes) |
| Execution from the HTTP API | ✅ Works, asynchronous | `POST /api/v1/runs` returns **202** with the run id and executes in a FastAPI background task; poll `GET /api/v1/runs/{id}` to `COMPLETED`/`FAILED`/`PAUSED`; JWT-protected; `input_data` stored for replay; LLM output mocked in tests |
| Replay | ✅ Works | `POST /api/v1/runs/{id}/replay` re-executes with stored inputs |
| Intent drafting (`POST /api/v1/intents/draft`) | ⚠️ Requires LLM | Calls OpenRouter; no offline test coverage |
| RAG context inside AI nodes | ⚠️ Best-effort | Retrieval is attempted when `ENABLE_RAG=true`; empty context on failure |
| Human approval steps | ⚠️ Simulated | Approval nodes auto-approve with a "system" signature; no pending-state loop yet |
| Rules resolution | ✅ Works | Rule logic resolved from `rules`/`rule_versions` tables at execution time (was hardcoded before) |
| Tests | ✅ `pytest -q` | In-process tests (`TestClient` + direct executor), LLM mocked |
| Config bootstrap | ✅ Works | `.env.example` included |
| Docker | ✅ Compose | Persistent volumes, entrypoint migrations, `/health` check, arm64 digests |
| CI | ✅ GitHub Actions | Tests + ruff correctness gate |
| Auth migrations | ⚠️ Manual | Existing local DBs: `alembic upgrade head`; containers migrate via entrypoint |

Security note: password hashing is SHA-256+salt (stdlib), not bcrypt. For a
prototype this is acceptable; for anything real, switch to bcrypt/argon2.

## Tech Stack

- **Backend**: FastAPI (Python 3.10+)
- **Database**: SQLite + SQLAlchemy (async), Alembic migrations
- **AI/LLM**: OpenRouter (any chat model, default configured via env)
- **Vector Store**: ChromaDB + HuggingFace embeddings
- **Orchestration**: LangGraph

## Setup

```bash
git clone https://github.com/tanmayd-dev/ai-workflow-platform.git
cd ai-workflow-platform

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
pip install -r requirements-dev.txt   # only needed for running the tests

cp .env.example .env                  # fill in OPENROUTER_API_KEY and SECRET_KEY
alembic upgrade head                  # only if a DB already exists; fresh DBs auto-create

python -m app.main
```

Server is at `http://localhost:8000`; Swagger UI at `http://localhost:8000/docs`
(the API itself lives under `http://localhost:8000/api/v1`).

For sample users/workflows/rules, run the one-time seeder: `python scripts/seed.py`
(creates the five RBAC roles and an admin user; it is idempotent, so re-running
it is safe). `.env` must be configured first, since the seed script imports the
app and needs `OPENROUTER_API_KEY`/`SECRET_KEY`.

## Roles and permissions

Authorization is enforced per endpoint by `require_roles(...)`
(`app/core/rbac.py`). Unknown role names are denied; matching is
case-insensitive. **A user with no role is denied every guarded endpoint with
403** — roles are never auto-assigned on registration. `admin` is a superuser
and passes every check.

| Role | Purpose |
|---|---|
| `admin` | Superuser; bypasses every role check (including admin-only actions) |
| `author` | Workflow/rule/knowledge authoring |
| `approver` | Human gates: approves workflow definitions and paused runs |
| `executor` | Starts and replays workflow runs |
| `auditor` | Read-only: definitions, runs, and audit trails |

| Endpoint | Required role |
|---|---|
| `POST /api/v1/workflows/draft` | `author` |
| `POST /api/v1/workflows/{id}/approve` | `approver` |
| `GET /api/v1/workflows/`, `GET /api/v1/workflows/{id}` | authenticated (no role) |
| `POST /api/v1/rules`, `POST /api/v1/rules/{id}/versions` | `author` |
| `DELETE /api/v1/rules/{id}` | `admin` |
| `GET /api/v1/rules`, `GET /api/v1/rules/{id}` | authenticated (no role) |
| `POST /api/v1/intents/draft` | `author` |
| `POST /api/v1/runs/`, `POST /api/v1/runs/{id}/replay` | `executor` |
| `POST /api/v1/runs/{id}/approve`, `POST /api/v1/runs/{id}/reject` | `approver` |
| `GET /api/v1/runs/{id}`, `GET /api/v1/runs/{id}/executions` | authenticated (no role) |
| `POST /api/v1/documents/upload` | `author` |
| `GET /api/v1/documents/`, `GET /api/v1/documents/{id}` | authenticated (no role) |
| `/api/v1/auth/register`, `/login`, `/me`, `/test-token` | none (public / token-only) |

Assign or revoke roles without SQL:

```bash
python scripts/assign_role.py --email alice@acmebank.com --role author
python scripts/assign_role.py --email alice@acmebank.com --role author --remove
```

The command is idempotent, never prints password hashes, and get-or-creates the
role row (so it also works before `scripts/seed.py`).

## Docker

```bash
cp .env.example .env        # fill in OPENROUTER_API_KEY, SECRET_KEY, POSTGRES_PASSWORD
docker compose up -d --build
```

Runtime state lives in named volumes rather than the image layer:

| Volume | Mount | Contents |
|---|---|---|
| `sqlite_data` | `/data/db` | `workflow_db.sqlite` |
| `chroma_data` | `/data/chroma` | ChromaDB index |
| `documents_data` | `/data/documents` | uploaded documents |

The container entrypoint creates and chowns those directories, runs
`alembic upgrade head`, then starts uvicorn as an unprivileged user. A fresh
volume therefore converges with no manual setup, and `AUTO_CREATE_SCHEMA=false`
prevents the app from duplicating schema creation at startup. The app has a
`/health` healthcheck and `restart: unless-stopped`. The Postgres service is
kept for deployments that switch `DATABASE_URL` to
`postgresql+asyncpg://...`; SQLite is the default.

## Tests

```bash
pytest -q
```

Tests run against a throwaway SQLite file via the in-process app; LLM calls are
mocked, so no API key is consumed. `scripts/` (root-level `test_*.py`, `demo*.py`,
`execute_*.py`) are manual smoke scripts from development — they hit a live
server at `127.0.0.1:8000` and are not part of the test suite.

## Project structure

```
app/
├── main.py                 # FastAPI entrypoint
├── core/                   # Config, database, security, RBAC
├── models/                 # SQLAlchemy models
├── schemas/                # Pydantic schemas
├── services/               # Business logic
│   ├── rag_service.py      # ChromaDB + embeddings
│   ├── llm_service.py      # OpenRouter client
│   ├── rule_engine.py      # JSON DSL evaluator
│   ├── compiler.py         # Workflow → LangGraph graph
│   └── executor.py         # Graph execution + persistence
└── api/v1/endpoints/       # REST API endpoints
```

## Known limitations / next steps

- Human-approval nodes pause execution in a real pending state and resume via POST /runs/{id}/approve or fail via POST /runs/{id}/reject.
- Auth is enforced on every endpoint: all reads require a JWT and mutations
  additionally require the role in the matrix above (workflow draft/approve,
  rule create/version/delete, run start/replay/approve/reject, document upload,
  intent drafting). `admin` bypasses role checks.
- The root-level scripts duplicate each other (4 API smoke scripts, 3 demos,
  2 seeders); consolidation is pending.
- Runs are enqueued with FastAPI `BackgroundTasks`: `POST /runs` returns 202
  immediately and execution happens after the response using its own DB
  session. Poll `GET /runs/{id}` for the outcome. A durable worker/queue is the
  next step for multi-process deployments. `approve`, `reject`, and `replay`
  remain synchronous.

## License

MIT

## Author

Tanmay Deshpande
