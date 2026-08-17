#!/usr/bin/env bash
# =============================================================================
# Regenerate gp_api_client from the backend OpenAPI spec.
#
# Usage:
#   ./mobile/scripts/regenerate_api_client.sh [--spec-url URL]
#
# Prerequisites:
#   - openapi-generator-cli installed (npm i -g @openapitools/openapi-generator-cli)
#   - Backend running or spec file available at SPEC_URL
#
# MASTER_PROMPT §1.1: clients are GENERATED, never hand-written.
# The hand-written infrastructure layer (token_storage, cert_pinning,
# gp_http_client) is NOT overwritten by this script.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOBILE_DIR="$(dirname "$SCRIPT_DIR")"
CLIENT_DIR="$MOBILE_DIR/gp_api_client"
GENERATED_DIR="$CLIENT_DIR/lib/src/generated"

SPEC_URL="${GP_API_SPEC_URL:-https://api.staging.genesisprestige.co.ke/v1/openapi.json}"

# Allow override via CLI flag.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --spec-url) SPEC_URL="$2"; shift 2 ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

echo "==> Fetching OpenAPI spec from: $SPEC_URL"
SPEC_FILE="$(mktemp /tmp/gp_openapi_XXXXXX.json)"
curl -fsSL "$SPEC_URL" -o "$SPEC_FILE"

echo "==> Generating Dart client into: $GENERATED_DIR"
mkdir -p "$GENERATED_DIR"

openapi-generator-cli generate \
  --input-spec "$SPEC_FILE" \
  --generator-name dart \
  --output "$GENERATED_DIR" \
  --additional-properties=pubName=gp_api_client_generated,pubVersion=0.1.0 \
  --skip-validate-spec

echo "==> Running dart format on generated code"
dart format "$GENERATED_DIR"

echo "==> Done. Commit the changes in $GENERATED_DIR."
rm -f "$SPEC_FILE"
