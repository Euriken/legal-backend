#!/usr/bin/env bash
# push_to_neon.sh — idempotent sync of local legaldb cases → Neon
# Usage:
#   NEON_URL="postgresql://..." bash push_to_neon.sh              # all cases
#   NEON_URL="postgresql://..." bash push_to_neon.sh "court_type='High Court'"
#
# Every INSERT is patched to ON CONFLICT DO NOTHING, so re-running
# this script after adding more cases will never fail on duplicates.

set -euo pipefail

NEON_URL="${NEON_URL:-}"
WHERE_CLAUSE="${1:-}"          # optional filter, e.g. "court_type='High Court'"
DUMP_FILE="/tmp/legaldb_push_$(date +%s).sql"
PATCHED_FILE="${DUMP_FILE%.sql}_patched.sql"

if [[ -z "$NEON_URL" ]]; then
  echo "ERROR: set NEON_URL environment variable first."
  echo "  export NEON_URL='postgresql://user:pass@host/dbname?sslmode=require'"
  exit 1
fi

echo "==> Dumping local cases..."

DUMP_ARGS=(
  -d legaldb
  --data-only
  -t cases
  --inserts                      # one INSERT per row (needed for sed patch)
  --no-comments
)

if [[ -n "$WHERE_CLAUSE" ]]; then
  echo "    Filter: WHERE $WHERE_CLAUSE"
  DUMP_ARGS+=(--where "$WHERE_CLAUSE")
fi

pg_dump "${DUMP_ARGS[@]}" -f "$DUMP_FILE"

ROW_COUNT=$(grep -c "^INSERT INTO" "$DUMP_FILE" || true)
echo "    Rows to push: $ROW_COUNT"

echo "==> Patching INSERT → INSERT ... ON CONFLICT DO NOTHING ..."
sed "s/^INSERT INTO \(cases\) /INSERT INTO \1 /;
     s/);$/)\n  ON CONFLICT DO NOTHING;/" "$DUMP_FILE" > "$PATCHED_FILE"

echo "==> Pushing to Neon..."
psql "$NEON_URL" \
  --set ON_ERROR_STOP=off \
  -q \
  -f "$PATCHED_FILE"

echo ""
echo "Done. Cleanup temp files? (y/N)"
read -r CLEAN
if [[ "${CLEAN,,}" == "y" ]]; then
  rm -f "$DUMP_FILE" "$PATCHED_FILE"
  echo "Temp files removed."
fi
