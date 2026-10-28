# Scripts

Manual helper scripts. They import the app or hit a live server, so `.env`
must be configured first. None of these are part of the test suite.

- `seed.py`: Idempotently seed the five RBAC roles and the admin user/role — `python scripts/seed.py`
- `assign_role.py`: Grant or revoke a role for a user (idempotent) — `python scripts/assign_role.py --email <email> --role <role> [--remove]`
- `demo.py`: **Authoritative authenticated walkthrough** of the platform against
  a live server: login with the seeded admin (overridable via `DEMO_EMAIL`/
  `DEMO_PASSWORD`), create an approver-role user via `assign_role.py`, seed the
  referenced rules through the rules API, create a rule/conditional/approval
  workflow with explicit list branch bodies, approve it with the approver,
  start a run, poll to `PAUSED`, approve the paused run, and print executions
  plus the run's `output_data`. Offline-capable: no AI step, no LLM/OpenRouter
  dependency — `python scripts/demo.py`
- `demo_all_nodes.py`: Manual live-session exercise of all node types
  (AI_ANALYSIS, DOMAIN_RULE, HUMAN_APPROVAL), hitting real LLM paths and
  sending no auth. **Deprecated in favor of demo.py**; see its header comment.
- `demo_intent_workflow.py`: Manual live-session exercise of document upload
  + AI workflow generation from intent (OpenRouter + RAG), sending no auth.
  **Deprecated in favor of demo.py**; see its header comment.
