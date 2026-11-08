#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:-.}"
PLAN="$ROOT/docs/planning/plan.md"
TRACE="$ROOT/docs/planning/traceability.md"
REVIEW="$ROOT/docs/planning/review.md"

[ -f "$PLAN" ] || { echo "missing $PLAN" >&2; exit 1; }
[ -f "$TRACE" ] || { echo "missing $TRACE" >&2; exit 1; }

echo "== plan headings =="
grep -n '^#\{1,3\} ' "$PLAN"

echo
echo "== traceability =="
cat "$TRACE"

echo
echo "== invariant lintas requirement =="
sed -n '/^## 2\. Invariant lintas requirement/,/^## 3\./p' "$TRACE"

if [ -f "$REVIEW" ]; then
  echo
  echo "== review lintas komponen =="
  sed -n '/^## 10\. Review lanjutan/,$p' "$REVIEW"
fi

echo
echo "== UAT anchors =="
grep -n 'UAT-[0-9]\+' "$PLAN"
