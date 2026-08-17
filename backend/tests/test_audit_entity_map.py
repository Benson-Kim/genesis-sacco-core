"""Review F4: ENTITY_MODULES completeness (deny-by-default, but never
silently forever).

application/audit_log.ENTITY_MODULES redacts payloads of any entity it
does not map — the right default for an exfiltration channel (gate
1.6), but drift-prone: a new audited entity would be redacted for every
caller with no test noticing. This suite statically scans
backend/src/genesis for every entity written to audit_log — both the
`entity=` keyword of the shared record_audit writer and the literal
entity column of the two direct INSERT writers (members.py, rbac.py) —
and asserts each one is mapped. Runs without a database on purpose: it
must fail in every pipeline, not only DB-backed ones.

Falsifiable: add `record_audit(..., entity="unmapped_probe", ...)`
anywhere under src/genesis (or drop a mapping) and the completeness
test fails naming the missing entity.
"""

import ast
import re
from pathlib import Path

from genesis.application.audit_log import ENTITY_MODULES

SRC = Path(__file__).resolve().parents[1] / "src" / "genesis"

_DIRECT_INSERT = re.compile(r"INSERT INTO audit_log\s*\(([^)]*)\)\s*VALUES", re.IGNORECASE)


def _split_top_level(csv: str) -> list[str]:
    """Split a SQL list on top-level commas (CAST(... AS ...) is safe)."""
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in csv:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


def _entities_from_sql(sql: str) -> list[str]:
    """Literal entity values of direct `INSERT INTO audit_log` strings.

    Locates the `entity` column position and reads the matching VALUES
    element; a quoted literal is collected, a bound parameter (the
    shared writer's own SQL) is covered by the call-site scan instead.
    """
    found: list[str] = []
    for match in _DIRECT_INSERT.finditer(sql):
        columns = [c.strip().lower() for c in _split_top_level(match.group(1))]
        if "entity" not in columns:
            continue
        tail = sql[match.end() :]
        open_paren = tail.index("(")
        depth = 0
        end = open_paren
        for i, ch in enumerate(tail[open_paren:], start=open_paren):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0:
                end = i
                break
        values = _split_top_level(tail[open_paren + 1 : end])
        value = values[columns.index("entity")]
        literal = re.fullmatch(r"'([^']*)'", value)
        if literal:
            found.append(literal.group(1))
    return found


def _audited_entities() -> tuple[set[str], list[str]]:
    """(entities, offences): every audit-written entity, plus any
    record_audit call whose entity= is not a static string literal
    (those cannot be verified against the map and are refused)."""
    entities: set[str] = set()
    offences: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                entities.update(_entities_from_sql(node.value))
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "record_audit":
                continue
            entity_kw = next((kw for kw in node.keywords if kw.arg == "entity"), None)
            if entity_kw is None:
                continue  # the writer's own definition
            if isinstance(entity_kw.value, ast.Constant) and isinstance(entity_kw.value.value, str):
                entities.add(entity_kw.value.value)
            else:
                offences.append(f"{path.relative_to(SRC)}:{node.lineno}")
    return entities, offences


def test_every_audited_entity_is_mapped_in_entity_modules() -> None:
    entities, offences = _audited_entities()
    assert not offences, (
        "record_audit called with a non-literal entity= — the redaction "
        f"map cannot be verified statically: {offences}"
    )
    # Sanity: the scan sees the known writers (a broken scanner must
    # never pass vacuously).
    assert {"users", "members", "permissions"} <= entities
    missing = sorted(entities - ENTITY_MODULES.keys())
    assert not missing, (
        "entities audited but missing from ENTITY_MODULES — their "
        "before/after payloads would be redacted for every caller "
        f"forever (review F4): {missing}"
    )
