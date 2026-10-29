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
| Auth (register/login/me) | ✅ Works | Argon2id hashes (legacy SHA-256 hashes upgrade transparently on next successful login), JWT bearer tokens bound to the `user_id` claim |
| Admin UI | ✅ Works | Single-page ops console at `GET /ui` — login, intent→draft, workflow review/approve, DRAFT-stage editing, run submission + live status, approval inbox, audit trail. No build step. |
| RBAC (roles on endpoints) | ✅ Enforced | `admin`/`author`/`approver`/`executor`/`auditor`; strict mapping (no role → 403), `admin` bypasses |
| Maker-checker (separation of duties) | ✅ Enforced | The run triggerer cannot approve/reject their own run, and the workflow author cannot approve their own draft (409); `admin` superusers are exempt |
| Document upload + RAG ingestion | ✅ Works | ChromaDB + HuggingFace embeddings (loaded lazily; boot no longer depends on a model download), per-domain collections, content-hash dedupe (409), upload size limit (413), threaded embeddings |
| Document management | ✅ Works | `DELETE /api/v1/documents/{id}` (admin) removes the DB row, file, and RAG chunks |
| Workflow CRUD (draft/approve/freeze) | ✅ Works | `created_by` is the real JWT caller id, recorded on create and on approval |
| Approve-time validation | ✅ Enforced | `POST /workflows/{id}/approve` compiles and validates first: zero steps, duplicate order/names, unresolved rule references, bad branch wiring → 422, stays DRAFT. `PATCH /workflows/{id}` lets authors edit DRAFT definitions (same validator as pre-check) |
| Workflow compilation | ✅ Works | Compiles to a real LangGraph `StateGraph` (AI / rule / approval / conditional nodes) |
| Conditional branch bodies | ✅ Works | `config_json.on_true`/`on_false` each take a list of step names `["A","B"]` wired exactly as listed (required for multi-step branch bodies); string entry-target form still works for single-step bodies and a contiguous earlier region with a single later step. Ambiguous layouts raise `ValueError` at compile time, so mistakes can't produce silently wrong wiring. |
| Execution from the HTTP API | ✅ Works, durable | `POST /api/v1/runs` returns **202**; the background task claims the run with a lease (CAS + heartbeat). A startup sweep fails runs left `RUNNING` by a crash ("interrupted by server restart"); restarts can't wedge an account. LLM output mocked in tests |
| Run discovery | ✅ Works | `GET /api/v1/runs?status=PAUSED&workflow_id=…&account_id=…&limit=&offset=` (newest first) — the approver inbox is a query, not a UUID handed over out of band |
| Cancel | ✅ Works | `POST /api/v1/runs/{id}/cancel` (`RUNNING`/`PAUSED`; admin or the triggering user); terminal states are final |
| Idempotency | ✅ Works | `Idempotency-Key` header on `POST /runs`: the same key returns the same run (200 instead of a duplicate decision). The one-active-run-per-account rule is enforced by a partial unique index |
| Rule-version pinning | ✅ Works | Run start pins `{rule: version}`; resume and replay re-execute against exactly those versions. A deleted pinned version fails loudly instead of silently upgrading |
| Run output visibility | ✅ Works | Terminal runs expose `output_data` in `GET /runs/{id}`: the final decision output — a dict for AI/approval steps, a scalar for rule steps. The same per-node inputs/outputs are in `GET /runs/{id}/executions`. |
| Human approval steps | ✅ Works | Approval nodes pause the run into a persisted `PAUSED` state with a pending node row; an approver resumes via `POST /api/v1/runs/{id}/approve` (compare-and-swap on the paused node row) or rejects via `POST /api/v1/runs/{id}/reject`. Not simulated. |
| Rules resolution | ✅ Works | Rule logic resolved from `rules`/`rule_versions` tables (steps reference rules by name; pinned-version semantics above) |
| Replay | ✅ Recorded mode / ⚠️ re-execute | `POST /runs/{id}/replay` with `{"mode": "recorded"}` replays the recorded outputs verbatim — no LLM call, deterministic, pinned rule versions. `{"mode": "re-execute"}` (default) re-runs live and can differ. Recorded mode requires a `COMPLETED` source run with every executed step recorded (409 otherwise) |
| Audit trail | ✅ Works | Run/node/approval events recorded with the real actor (approver attributed correctly, not the triggerer). Readable at `GET /api/v1/audit-logs` (`auditor`/`admin`), filterable by entity |
| Intent drafting (`POST /api/v1/intents/draft`) | ⚠️ Requires LLM | Calls OpenRouter; covered offline by 5 tests in `tests/test_intent_drafting.py` (LLM mocked) |
| RAG context inside AI nodes | ⚠️ Best-effort | Retrieval is attempted when `ENABLE_RAG=true`; empty context on failure |
| Observability | ✅ Basic | Structured logging (`LOG_LEVEL`, `LOG_JSON`), `X-Request-ID` middleware, `GET /ready` probes the DB (503 on failure) |
| Tests | ✅ `pytest -q` | 164 tests. In-process tests (`TestClient` + direct executor), LLM mocked; `test_rag_service.py` is the exception — it exercises real ChromaDB and a real local HuggingFace embedding model (downloads ~88 MB on first run) |
| Offline E2E | ✅ `python scripts/e2e_check.py` | Walks seed → login → rules → draft → approve → run → pause → approve → output → audit in-process with no LLM and no server (9 stages) |
| Config bootstrap | ✅ Works | `.env.example` included; `SECRET_KEY` validated (≥ 32 chars); `DEBUG` gates SQL echo; `CORS_ORIGINS` opt-in CSV |
| Login hardening | ✅ Works | Login rate limit: 5 failed attempts per 5 minutes (IP+email) → 429 with `Retry-After`; process-local (single-process deployments) |
| Docker | ✅ Compose | Persistent volumes, entrypoint migrations + seeded roles/admin (`SEED_ON_STARTUP`, random one-time `BOOTSTRAP_ADMIN_PASSWORD` printed to logs if unset), `/health` + `/ready`, arm64 digests |
| CI | ✅ GitHub Actions | pytest (HF model cached), ruff gate incl. `scripts/`, `alembic` upgrade→downgrade→upgrade cycle job, `docker build` + compose config job |
| Auth migrations | ⚠️ Manual | Existing local DBs: `alembic upgrade head`; containers migrate via entrypoint |

## Known limitations

- **Single-tenant / global reads**: every authenticated user can read all workflows, rules, runs, and audit artifacts — `domain` is a label, not a boundary.
- **Runs execute in-process**: execution is lease-claimed inside the API process; multi-worker/multi-replica deployments have no queue — one process should run the fleet for now, and a durable worker is the next step.
- **No pagination on some lists**: workflows/rules/documents lists are unbounded; `GET /runs` and `GET /audit-logs` support limit/offset.
- **Pagination aside, some runs are synchronous**: `approve`, `reject`, and `replay` complete inside the HTTP request of this process — a long post-approval leg can hold the request.
- **Documents are UTF-8 text only** (5 MB cap); no PDF/DOCX parsing.
- **Replay default re-executes live** (LLM call, current rule versions); use `mode: "recorded"` for determinism.
- **`scripts/demo_all_nodes.py` / `demo_intent_workflow.py` are manual live-scripts** (deprecated in favor of `demo.py`, which needs no LLM).
- **No write-time DSL validation for rules** — malformed `logic_json` surfaces at run time.
- **Prompt-injection defenses are basic**: document text is injected as system context; delimiters/sanitization only.

Security note: passwords are Argon2id. Legacy SHA-256+salt hashes from older versions verify and are transparently rehashed at next successful login.

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
alembic upgrade head                  # if a DB already exists; brings it up to head
                                      # (4 pending migrations since a3d9f2c4e8b1:
                                      # output_data, leases/sweep, cancel+idempotency
                                      # indexes, rule_versions_pinned)

python -m app.main
```

Fresh databases are created automatically; `alembic upgrade head` is for
updating an existing local DB.

Server is at `http://localhost:8000`; Swagger UI at `http://localhost:8000/docs`
(the API itself lives under `http://localhost:8000/api/v1`), and the ops console
at `http://localhost:8000/ui`.

For sample users/rules, run the one-time seeder: `python scripts/seed.py`
(creates the five RBAC roles and an admin user plus demo rules by default; it
is idempotent, so re-running it is safe). `.env` must be configured first,
since the seed script imports the app and needs
`OPENROUTER_API_KEY`/`SECRET_KEY`. For a no-LLM sanity check of the whole
loop, run `python scripts/e2e_check.py`.

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

## License

MIT

## Author

Tanmay Deshpande
