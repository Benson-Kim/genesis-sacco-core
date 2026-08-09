#!/bin/sh
# Client drift-check (P14 EXIT criterion).
#
# Regenerates the TypeScript API client from the committed OpenAPI snapshot
# (packages/api-client/openapi.json) into a temp dir and fails if the result
# differs from the committed generated client. Paired with the `web:spec-drift`
# CI job (backend spec vs committed snapshot), this guarantees the client can
# never silently drift from the backend contract (MASTER_PROMPT §2.1: clients
# are GENERATED, never hand-written).
set -eu

cd "$(dirname "$0")/.."

SNAPSHOT="packages/api-client/src/generated/schema.d.ts"
SPEC="packages/api-client/openapi.json"

if [ ! -f "$SNAPSHOT" ]; then
  echo "ERROR: $SNAPSHOT missing — run 'npm run generate:api' and commit it." >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

npx openapi-typescript "$SPEC" -o "$TMP_DIR/schema.d.ts" >/dev/null

if ! diff -u "$SNAPSHOT" "$TMP_DIR/schema.d.ts"; then
  echo "" >&2
  echo "ERROR: generated API client is STALE." >&2
  echo "Run 'npm run generate:api' in web/ and commit the result." >&2
  exit 1
fi

echo "OK: generated API client matches the committed OpenAPI snapshot."
