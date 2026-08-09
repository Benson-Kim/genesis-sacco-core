"""Narrative-hygiene gate for the API description surface.

The OpenAPI contract is product surface: its description/summary/title
fields render in client docs and generated-code tooltips. Engineering
provenance (work-item ids, MR ids, batch numbers, audit/finding codes,
plan codes, doctrine citations) must never appear there. This gate scans

1. every ``description``/``summary``/``title`` string in the committed
   OpenAPI snapshot (``web/packages/api-client/openapi.json`` — the
   spec-drift job guarantees the snapshot matches the backend), and
2. every route-function and model-class docstring in
   ``src/genesis/api/`` (the sources those spec fields are rendered
   from), so an offender is caught at its source file too.

The banned list is code-owned. The web twin of this gate is
``web/src/__tests__/copy-provenance.test.ts`` (the rendered-copy
surface) — keep the two lists aligned when extending either.

Judgement calls (documented so the list stays honest):
- Issue refs are ``#`` + 1-2 digits with no hex digit following (hex
  colour literals are not provenance).
- Bare review codes ``R<n>``/``A<n>`` are NOT gated standalone — they
  collide with real-world tokens; review provenance travels with the
  other gated codes.
- ``GP-`` is the member-number format, NOT a plan code — never ban it.

Falsifiability: every banned pattern must flag its synthetic offender
sample below — removing a pattern (or weakening its regex) fails that
leg, so the gate can never pass vacuously.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
API_SRC = BACKEND_ROOT / "src" / "genesis" / "api"
OPENAPI_SNAPSHOT = REPO_ROOT / "web" / "packages" / "api-client" / "openapi.json"

BANNED: dict[str, re.Pattern[str]] = {
    "work-item reference": re.compile(r"(?<![\w&#])#\d{1,2}(?![\dA-Fa-f])|\bissue\s*#\d+", re.I),
    "merge-request reference": re.compile(r"(?<![\w!])!\d{1,3}(?!\d)"),
    "batch number": re.compile(r"\bbatch[ -]\d+\b", re.I),
    "audit finding code": re.compile(
        r"\bDA-\d+|\bB13-R\d\b|\bFM\d\b|\bN[1-9]\b|\bU[1-6]\b|\bT5\b|\bW[1-4]\b|\bX[34]\b"
    ),
    "plan code": re.compile(r"\bP1[0-9]\.\d+\b|\bP15\b|\bP22\b"),
    "doctrine citation": re.compile(
        r"\brule 1[0-9]\b|\bgate 1\.[1-6]\b|MASTER_PROMPT|BUILD_PROMPTS|§\d", re.I
    ),
    "process vocabulary": re.compile(
        r"\bledger \([a-o]\)|\baudit #|\bprototype\b|\bhand-off\b|\bre-import\b", re.I
    ),
}

OFFENDER_SAMPLES: dict[str, str] = {
    "work-item reference": "Follow-up recorded on issue #31",
    "merge-request reference": "Fixed in !52 review",
    "batch number": "Added by batch 8 remediation",
    "audit finding code": "Per finding B13-R4 and FM2",
    "plan code": "P13.5 frontend branch scope",
    "doctrine citation": "Least disclosure (gate 1.6) applies",
    "process vocabulary": "Matches the prototype exit checklist",
}

_DESCRIPTION_KEYS = frozenset({"description", "summary", "title"})


def _iter_spec_strings(node: object, path: str = "") -> list[tuple[str, str]]:
    """Yield (json-path, value) for every description-surface string."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}/{key}"
            if key in _DESCRIPTION_KEYS and isinstance(value, str):
                found.append((here, value))
            found.extend(_iter_spec_strings(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_iter_spec_strings(value, f"{path}/{index}"))
    return found


def _iter_api_docstrings() -> list[tuple[str, str]]:
    """Yield (location, docstring) for every class/function in genesis.api."""
    found: list[tuple[str, str]] = []
    for source_file in sorted(API_SRC.rglob("*.py")):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                docstring = ast.get_docstring(node)
                if docstring:
                    location = f"{source_file.relative_to(BACKEND_ROOT)}:{node.name}"
                    found.append((location, docstring))
    return found


def test_gate_is_falsifiable() -> None:
    """Every banned pattern flags its synthetic offender (non-vacuity)."""
    assert set(OFFENDER_SAMPLES) == set(BANNED)
    for name, pattern in BANNED.items():
        assert pattern.search(OFFENDER_SAMPLES[name]), name


def test_scan_inputs_are_non_trivial() -> None:
    """The gate reads a real spec and a real source tree, never nothing."""
    assert len(_iter_spec_strings(json.loads(OPENAPI_SNAPSHOT.read_text()))) > 100
    assert len(_iter_api_docstrings()) > 50


def test_openapi_description_surface_is_provenance_free() -> None:
    spec = json.loads(OPENAPI_SNAPSHOT.read_text(encoding="utf-8"))
    offenders = [
        f"{path}: [{name}] {value[:100]!r}"
        for path, value in _iter_spec_strings(spec)
        for name, pattern in BANNED.items()
        if pattern.search(value)
    ]
    assert offenders == []


def test_api_route_and_model_docstrings_are_provenance_free() -> None:
    offenders = [
        f"{location}: [{name}] {docstring[:100]!r}"
        for location, docstring in _iter_api_docstrings()
        for name, pattern in BANNED.items()
        if pattern.search(docstring)
    ]
    assert offenders == []
