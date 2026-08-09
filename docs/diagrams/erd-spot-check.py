#!/usr/bin/env python3
"""P-DIAG.2 spot-check: no invented tables in the ERD, no missing ones.

Run from the repository root:

    python3 docs/diagrams/erd-spot-check.py

Checks (stdlib only, no dependencies), BOTH ways:

1. Every entity drawn in an ``erDiagram`` block of erd.md and every
   table named in the S3 traceability table's first column is a table
   actually created by ``CREATE TABLE`` in
   ``backend/migrations/versions/*.py`` (the stronger form of "appears
   in the migrations": a name that merely occurs in prose but was
   never created still FAILs).
2. Every ``CREATE TABLE`` table in the migrations appears in erd.md
   both as a drawn entity and as a traceability row — a new migration
   table missing from the ERD is a FAIL (v1.2 rule 11 drift gate).

This script is the falsifiable half of the P-DIAG.2 EXIT criterion:
the CI ``docs:diagrams`` render job is a SYNTAX gate only; the
semantics (real tables, complete coverage) are pinned by this check
plus the per-edge FK citations in the diagrams (the P-DIAG.1
c4-spot-check.py pattern). It deliberately does NOT touch
c4-spot-check.py or any other diagram's gate.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ERD_MD = REPO_ROOT / "docs/diagrams/erd.md"
VERSIONS_DIR = REPO_ROOT / "backend/migrations/versions"

# Requires the opening parenthesis of a column list so that PROSE in a
# migration docstring (e.g. 0027's "CREATE TABLE takes no lock on ...")
# is never miscounted as a created table.
CREATE_TABLE_RE = re.compile(r"\bCREATE TABLE (\w+)\s*\(")
MERMAID_FENCE_RE = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
ENTITY_BLOCK_RE = re.compile(r"^\s*(\w+)\s*\{\s*$")
RELATIONSHIP_RE = re.compile(r"^\s*(\w+)\s+[|}o]{2}--[|{o]{2}\s+(\w+)\s*:")
BARE_ENTITY_RE = re.compile(r"^\s*(\w+)\s*$")
TRACE_ROW_RE = re.compile(r"^\| `(\w+)` \|", re.MULTILINE)
MERMAID_KEYWORDS = {"erDiagram"}


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def migration_tables() -> set[str]:
    files = sorted(VERSIONS_DIR.glob("*.py"))
    if not files:
        fail(f"no migration files found under {VERSIONS_DIR}")
    tables: set[str] = set()
    for f in files:
        tables.update(CREATE_TABLE_RE.findall(f.read_text(encoding="utf-8")))
    if not tables:
        fail("no CREATE TABLE statements found in the migrations — regex drift?")
    return tables


def erd_entities(text: str) -> set[str]:
    blocks = MERMAID_FENCE_RE.findall(text)
    if not blocks:
        fail(f"no ```mermaid blocks found in {ERD_MD}")
    entities: set[str] = set()
    for block in blocks:
        in_attributes = False
        for line in block.splitlines():
            if in_attributes:
                if line.strip() == "}":
                    in_attributes = False
                continue
            m = ENTITY_BLOCK_RE.match(line)
            if m:
                entities.add(m.group(1))
                in_attributes = True
                continue
            m = RELATIONSHIP_RE.match(line)
            if m:
                entities.update(m.groups())
                continue
            m = BARE_ENTITY_RE.match(line)
            if m and m.group(1) not in MERMAID_KEYWORDS:
                entities.add(m.group(1))
    return entities


def main() -> None:
    if not ERD_MD.exists():
        fail(f"missing input file {ERD_MD}")
    real = migration_tables()
    text = ERD_MD.read_text(encoding="utf-8")
    drawn = erd_entities(text)
    traced = set(TRACE_ROW_RE.findall(text))
    if not traced:
        fail("no traceability rows (| `table` |) found in erd.md — format drift?")

    # --- Direction 1: nothing cited that the migrations never created ---
    invented_drawn = sorted(drawn - real)
    if invented_drawn:
        fail(
            "entities drawn in erd.md that no migration CREATE TABLEs:\n  "
            + "\n  ".join(invented_drawn)
        )
    invented_traced = sorted(traced - real)
    if invented_traced:
        fail(
            "traceability rows in erd.md that no migration CREATE TABLEs:\n  "
            + "\n  ".join(invented_traced)
        )

    # --- Direction 2: nothing created that erd.md omits ----------------
    undrawn = sorted(real - drawn)
    if undrawn:
        fail(
            "tables created by the migrations but not drawn in any erd.md "
            "erDiagram:\n  " + "\n  ".join(undrawn)
        )
    untraced = sorted(real - traced)
    if untraced:
        fail(
            "tables created by the migrations but missing a traceability "
            "row in erd.md:\n  " + "\n  ".join(untraced)
        )

    print(
        f"OK: {len(real)} tables created by the migrations; all {len(drawn)} "
        f"drawn erd.md entities and all {len(traced)} traceability rows are "
        "real tables; coverage is complete both ways."
    )


if __name__ == "__main__":
    main()
