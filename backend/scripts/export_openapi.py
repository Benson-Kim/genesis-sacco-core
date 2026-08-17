"""Export the FastAPI OpenAPI contract as deterministic JSON.

Used by the P14 web client drift-check (`web:spec-drift` CI job): the web
API client is GENERATED from this contract (MASTER_PROMPT 2.1/2.3), and the
committed snapshot at `web/packages/api-client/openapi.json` must always
match what the backend actually serves.

Usage: python scripts/export_openapi.py <output-path|->
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from genesis.api.app import create_app


def render_spec() -> str:
    """Serialize the app's OpenAPI spec with stable key ordering."""
    spec = create_app().openapi()
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: export_openapi.py <output-path|->\n")
        return 2
    payload = render_spec()
    if argv[1] == "-":
        sys.stdout.write(payload)
    else:
        destination = Path(argv[1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
