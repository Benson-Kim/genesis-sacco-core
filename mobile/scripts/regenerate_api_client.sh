#!/usr/bin/env bash
# Regenerate gp_api_client's model layer from the committed OpenAPI snapshot.
#
# Replaces the P16 script, which pointed at a staging URL that does not exist
# (scaffold defect D3). The binding contract is the snapshot in the repo:
# web/packages/api-client/openapi.json, already arbitrated against the running
# backend by web:spec-drift.
#
# Usage:
#   mobile/scripts/regenerate_api_client.sh          # regenerate in place
#   mobile/scripts/regenerate_api_client.sh --check  # fail if the tree drifts
#
# The generated tree is NEVER hand-edited — the same hard rule that governs
# web/packages/api-client/src/generated/schema.d.ts. mobile:codegen-drift runs
# this with --check and a diff is a red pipeline.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SPEC="${REPO_ROOT}/web/packages/api-client/openapi.json"
PACKAGE="${REPO_ROOT}/mobile/gp_api_client"
GENERATED="${PACKAGE}/lib/src/generated"
CONFIG="${PACKAGE}/openapi-generator-config.yaml"
IMAGE="openapitools/openapi-generator-cli:v7.4.0"

if [[ ! -f "${SPEC}" ]]; then
  echo "error: OpenAPI snapshot not found at ${SPEC}" >&2
  exit 1
fi

CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
fi

generate() {
  local out="$1"
  # Docker is the only supported runner: pinning the generator IMAGE pins the
  # generator VERSION, and an unpinned generator silently rewrites the whole
  # tree on every upgrade, which turns the drift job into noise.
  if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker is required to regenerate the client (image ${IMAGE})." >&2
    echo "       CI provides it; locally, install Docker or let mobile:codegen-drift arbitrate." >&2
    exit 2
  fi
  docker run --rm \
    -v "${REPO_ROOT}:/work" \
    -w /work \
    "${IMAGE}" generate \
    -i "web/packages/api-client/openapi.json" \
    -c "mobile/gp_api_client/openapi-generator-config.yaml" \
    -o "${out#"${REPO_ROOT}/"}"
}

if [[ "${CHECK_ONLY}" -eq 1 ]]; then
  SCRATCH="$(mktemp -d)"
  trap 'rm -rf "${SCRATCH}"' EXIT
  cp -r "${PACKAGE}" "${SCRATCH}/gp_api_client"
  rm -rf "${SCRATCH}/gp_api_client/lib/src/generated"
  generate "${PACKAGE}"
  if ! git -C "${REPO_ROOT}" diff --exit-code -- "mobile/gp_api_client/lib/src/generated"; then
    echo >&2
    echo "error: the generated client is stale or hand-edited." >&2
    echo "       Run mobile/scripts/regenerate_api_client.sh and commit the result." >&2
    exit 1
  fi
  echo "gp_api_client generated tree is current."
else
  rm -rf "${GENERATED}"
  generate "${PACKAGE}"
  echo "Regenerated ${GENERATED}"
  echo "Review the diff, then commit. Never hand-edit this tree."
fi
