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
| Document upload + RAG ingestion | ✅ Works | ChromaDB + HuggingFace embeddings, per-domain collections |
| Workflow CRUD (draft/approve/freeze) | ✅ Works | `created_by` is still a placeholder UUID, not the caller |
| Workflow | ✅ Works | Compiles to a real LangGraph `StateGraph` (AI / rule / approval / conditional nodes) |
| Execution from the HTTP API | ✅ Works, synchronous | `POST /api/v1/runs` drives the executor; JWT-protected; `input_data` stored for replay; LLM output mocked in tests |
| Replay | ✅ Works | `POST /api/v1/runs/{id}/replay` re-executes with stored inputs |
| Intent drafting (`POST /api/v1/intents/draft`) | ⚠️ Requires LLM | Calls OpenRouter; no offline test coverage |
| RAG context inside AI nodes | ⚠️ Best-effort | Retrieval is attempted when `ENABLE_RAG=true`; empty context on failure |
| Human approval steps | ⚠️ Simulated | Approval nodes auto-approve with a "system" signature; no pending-state loop yet |
| Rules resolution | ✅ Works | Rule logic resolved from `rules`/`rule_versions` tables at execution time (was hardcoded before) |
| Tests | ✅ `pytest -q` | In-process tests (`TestClient` + direct executor), LLM mocked |
| Config bootstrap | ✅ Works | `.env.example` included |
| Docker / CI | ❌ None | No Dockerfile, no CI pipeline |
| Auth migrations | ⚠️ Manual | `alembic upgrade head` for existing DBs; fresh DBs via `startup` |

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
(roles + admin user). `.env` must be configured first, since the
seed script imports the app and needs `OPENROUTER_API_KEY`/`SECRET_KEY`.

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
├── core/                   # Config, database, security
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
- Auth is enforced on `/runs` plus workflow draft/approve and document upload;
  remaining endpoints gain auth as they are added (rules next).
- The root-level scripts duplicate each other (4 API smoke scripts, 3 demos,
  2 seeders); consolidation is pending.
- Runs execute synchronously inside the HTTP request; long executions belong in
  a background task/queue.
- No CI tracking.

## License

MIT

## Author

Tanmay Deshpande
