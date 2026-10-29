#!/usr/bin/env bash
# Exercise the full migration cycle against a scratch database:
#   alembic upgrade head -> downgrade base -> upgrade head
# Any failed step exits nonzero (set -e). Used by CI and as a local gate.
#
# Environment overrides:
#   PYTHON        interpreter that has alembic + app deps (default: python)
#   DATABASE_URL  target database (default: scratch sqlite file, deleted after)
# SECRET_KEY / OPENROUTER_API_KEY are only set (to dummies) if not already
# present — alembic/env.py imports app.core.config, whose Settings require them.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python}"

# Scratch database, removed after the cycle unless the caller supplied their own.
if [ -z "${DATABASE_URL:-}" ]; then
    SCRATCH_URL="sqlite+aiosqlite:///./.check_migrations_tmp.db"
    SCRATCH_FILE="./.check_migrations_tmp.db"
else
    SCRATCH_URL=""
    SCRATCH_FILE=""
fi

export DATABASE_URL="${DATABASE_URL:-$SCRATCH_URL}"
export SECRET_KEY="${SECRET_KEY:-ci-scratch-migration-secret}"
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-ci-scratch-migration-key}"

echo "[check_migrations] python=$PYTHON db=$DATABASE_URL"
"$PYTHON" -m alembic upgrade head
"$PYTHON" -m alembic downgrade base
"$PYTHON" -m alembic upgrade head

if [ -n "$SCRATCH_FILE" ]; then
    rm -f "$SCRATCH_FILE" "$SCRATCH_FILE-wal" "$SCRATCH_FILE-shm"
fi
echo "[check_migrations] cycle OK"
