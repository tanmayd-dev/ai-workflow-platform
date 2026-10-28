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
| Workflow CRUD (draft/approve/freeze) | ✅ Works | `created_by` is the real JWT caller id, recorded on create and on approval |
| Workflow compilation | ✅ Works | Compiles to a real LangGraph `StateGraph` (AI / rule / approval / conditional nodes) |
| Conditional branch bodies | ✅ Works | `config_json.on_true`/`on_false` each take a list of step names `["A","B"]` wired exactly as listed (required for multi-step branch bodies); string entry-target form still works for single-step bodies and a contiguous earlier region with a single later step. Ambiguous layouts raise `ValueError` at compile time, so mistakes can't produce silently wrong wiring. |
| Execution from the HTTP API | ✅ Works, asynchronous | `POST /api/v1/runs` returns **202** with the run id and executes in a FastAPI background task; poll `GET /api/v1/runs/{id}` to `COMPLETED`/`FAILED`/`PAUSED`; JWT-protected; `input_data` stored for replay; LLM output mocked in tests |
| Run output visibility | ✅ Works | Terminal runs expose `output_data` in `GET /runs/{id}`: the final decision output — a dict for AI/approval steps, a scalar for rule steps. The same per-node inputs/outputs are in `GET /runs/{id}/executions`. |
| Human approval steps | ✅ Works | Approval nodes pause the run into a persisted `PAUSED` state with a pending node row; an approver resumes via `POST /api/v1/runs/{id}/approve` (compare-and-swap on the paused node row) or rejects via `POST /api/v1/runs/{id}/reject`. Not simulated. |
| Rules resolution | ✅ Works | Rule logic resolved from `rules`/`rule_versions` tables at execution time (steps reference rules by name; the executor resolves the latest active version) |
| Replay | ⚠️ Works, not deterministic | `POST /api/v1/runs/{id}/replay` re-executes with stored inputs — it re-calls the LLM and re-resolves the latest active rule versions, so it is not a deterministic replay. Allowed only for `COMPLETED` runs. |
| Intent drafting (`POST /api/v1/intents/draft`) | ⚠️ Requires LLM | Calls OpenRouter; covered offline by 5 tests in `tests/test_intent_drafting.py` (LLM mocked) |
| RAG context inside AI nodes | ⚠️ Best-effort | Retrieval is attempted when `ENABLE_RAG=true`; empty context on failure |
| Tests | ✅ `pytest -q` | 97 tests. In-process tests (`TestClient` + direct executor), LLM mocked; `test_rag_service.py` is the exception — it exercises real ChromaDB and a real local HuggingFace embedding model (downloads ~88 MB on first run) |
| Config bootstrap | ✅ Works | `.env.example` included; `DEBUG` gates SQL echo (off by default) and `CORS_ORIGINS` is an opt-in CSV of explicit browser origins (no CORS middleware when unset) |
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
alembic upgrade head                  # if a DB already exists; brings it up to head, including a3d9f2c4e8b1 (adds workflow_runs.output_data)

python -m app.main
```

Fresh databases are created automatically; `alembic upgrade head` is for
updating an existing local DB — migration `a3d9f2c4e8b1` adds the
`output_data` column to `workflow_runs`.

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

97 tests run against a throwaway SQLite file via the in-process app; LLM calls
are mocked, so no API key is consumed. `test_rag_service.py` is the exception:
it exercises real ChromaDB and a real local HuggingFace embedding model and
downloads the ~88 MB model on first run.

The demo scripts under `scripts/` (root-level experiment scripts were
consolidated there long ago) hit a live server at `127.0.0.1:8000` and are not
part of the test suite. `scripts/demo.py` is the authoritative authenticated
walkthrough — it is offline-capable (no AI step, no LLM call).

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

## Known limitations

Last reviewed against the code on this branch; the fixes whose absence used to
be listed here (real human-approval pause/resume, real `created_by`, offline
intent-drafting tests, script consolidation, `GET /documents/{id}`, run
`output_data`, multi-step conditional branch bodies, `DEBUG`/`CORS_ORIGINS`
handling) have landed and are covered by the table above.

- Runs execute via FastAPI `BackgroundTasks`, which is **not durable**: there
  is no worker/queue, so a process crash or restart leaves matching `RUNNING`
  rows stranded and `PAUSED` runs resume only because they are persisted. A
  durable worker is the next step for multi-process deployments. `approve`,
  `reject`, and `replay` remain synchronous.
- If the background executor dies mid-run, an approve/reject on the stranded
  pause can return **500** (rather than 200 with a FAILED body).
- Replay is not deterministic: it re-executes the graph — re-calls the LLM and
  re-resolves the **latest active** rule version per step rather than the
  versions used in the original run. Replay is allowed only for `COMPLETED`
  runs.
- No `GET /runs` list endpoint yet (only per-run fetch); no audit-log API yet.
- Rule DSL has no write-time validation — malformed `logic_json` surfaces only
  at execution time.
- Approver separation is not enforced: an approver **may approve a run they
  triggered themselves** (maker-checker is not yet implemented).
- Password hashing is SHA-256+salt (stdlib), not bcrypt/argon2.
- Uploaded documents: UTF-8 text only, no size limit, and no delete endpoint.
- Single-tenant: all reads (workflows, rules, runs, documents) are global to
  any authenticated user.
- No pagination on list endpoints.

## License

MIT

## Author

Tanmay Deshpande
