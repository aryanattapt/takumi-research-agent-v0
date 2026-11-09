#!/usr/bin/env bash
# One local gate for a Takumi change: compile, backend tests, migration roundtrip, dashboard build.
# Usage: bash .semutcode/skills/jalur/takumi-resume-milestone/scripts/verify-local.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../.." && pwd)"
cd "$REPO_ROOT"
echo "== repo: $REPO_ROOT"

echo "== compile"
python -m compileall -q src tests

echo "== backend tests (package is not pip-installed locally, so PYTHONPATH=src)"
PYTHONPATH=src python -m pytest

echo "== alembic roundtrip on throwaway sqlite (CI runs 'alembic upgrade head' on sqlite)"
# Keep the file inside the repo: native Python cannot open Git Bash /tmp paths on Windows.
TMP_DB="./_tmp_migrations_verify.db"
rm -f "$TMP_DB"
trap 'rm -f "$REPO_ROOT/$TMP_DB"' EXIT
export DATABASE_URL="sqlite:///$TMP_DB"
PYTHONPATH=src python -m alembic upgrade head
PYTHONPATH=src python -m alembic downgrade base
PYTHONPATH=src python -m alembic upgrade head
rm -f "$TMP_DB"

echo "== dashboard build (tsc -b && vite build)"
if [ -d web/node_modules ]; then
  (cd web && npm run build)
else
  echo "SKIP: web/node_modules missing, run 'npm --prefix web ci' first"
fi

echo "== OK"
