#!/usr/bin/env python3
"""P-DIAG.1 spot-check: no invented boxes in the C4 diagrams.

Run from the repository root:

    python3 docs/diagrams/c4-spot-check.py

Checks (stdlib only, no dependencies):

1. Every ``genesis/...`` module path cited in c4-context.md,
   c4-container.md and c4-component.md exists under
   ``backend/src/`` — an untraceable box is a rejected MR
   (PHASE B2 common rules: never invent structure).
2. Router completeness, both directions: the set of router modules
   wired via ``include_router`` in ``genesis/api/app.py`` equals the
   set of ``genesis/api/*.py`` router modules cited in
   c4-component.md.

This script is the falsifiable half of the P-DIAG.1 EXIT criterion:
the CI ``docs:diagrams`` render job is a SYNTAX gate only; the
semantics (real modules, real routers) are pinned by this check plus
the per-element code citations in the companion tables.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DIAGRAMS = [
    REPO_ROOT / "docs/diagrams/c4-context.md",
    REPO_ROOT / "docs/diagrams/c4-container.md",
    REPO_ROOT / "docs/diagrams/c4-component.md",
]
BACKEND_SRC = REPO_ROOT / "backend/src"
APP_PY = BACKEND_SRC / "genesis/api/app.py"
COMPONENT_MD = REPO_ROOT / "docs/diagrams/c4-component.md"

# A cited module path: genesis/<pkg>/<module>.py (also matches bare
# genesis/api etc. only when the .py suffix is present — package
# citations like "genesis/domain" are checked as directories below).
MODULE_RE = re.compile(r"\bgenesis/[a-z_/]+\.py\b")
PACKAGE_RE = re.compile(r"\bgenesis/[a-z_]+(?=[^./a-z_]|$)")
INCLUDE_RE = re.compile(r"from genesis\.api\.([a-z_]+) import router")


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    for p in [*DIAGRAMS, APP_PY]:
        if not p.exists():
            fail(f"missing input file {p}")

    # --- Check 1: every cited module path exists -----------------------
    cited_modules: set[str] = set()
    cited_packages: set[str] = set()
    for diagram in DIAGRAMS:
        text = diagram.read_text(encoding="utf-8")
        cited_modules.update(MODULE_RE.findall(text))
        cited_packages.update(PACKAGE_RE.findall(text))
    missing = sorted(m for m in cited_modules if not (BACKEND_SRC / m).is_file())
    if missing:
        fail("cited module paths that do not exist on this tree:\n  " + "\n  ".join(missing))
    missing_pkgs = sorted(p for p in cited_packages if not (BACKEND_SRC / p).is_dir())
    if missing_pkgs:
        fail("cited package paths that do not exist:\n  " + "\n  ".join(missing_pkgs))

    # --- Check 2: router completeness, both directions -----------------
    app_text = APP_PY.read_text(encoding="utf-8")
    wired = set(INCLUDE_RE.findall(app_text))
    if not wired:
        fail(f"no include_router imports found in {APP_PY} — regex drift?")
    cited_routers = {
        m.removeprefix("genesis/api/").removesuffix(".py")
        for m in MODULE_RE.findall(COMPONENT_MD.read_text(encoding="utf-8"))
        if m.startswith("genesis/api/")
        and m
        not in (
            # Seam modules, not router groups (diagram 0):
            "genesis/api/app.py",
            "genesis/api/idempotency.py",
            "genesis/api/authz.py",
            "genesis/api/params.py",
        )
    }
    undocumented = sorted(wired - cited_routers)
    if undocumented:
        fail(
            "routers wired in app.py but missing an L3 diagram in "
            "c4-component.md:\n  " + "\n  ".join(undocumented)
        )
    invented = sorted(cited_routers - wired)
    if invented:
        fail(
            "L3 diagrams cite router modules NOT wired in app.py:\n  " + "\n  ".join(invented)
        )

    print(
        f"OK: {len(cited_modules)} cited module paths exist; "
        f"{len(wired)} routers wired in app.py all have L3 diagrams; "
        "no invented routers."
    )


if __name__ == "__main__":
    main()
