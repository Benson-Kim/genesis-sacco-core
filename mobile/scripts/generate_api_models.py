#!/usr/bin/env python3
"""Generate the member-audience Dart models from the committed OpenAPI snapshot.

Why this exists instead of openapi-generator
--------------------------------------------
Two reasons, both discovered by running the standard tool in CI first:

1. **It crashes on this spec.** FastAPI emits OpenAPI 3.1, and
   openapi-generator 7.4.0's Dart generator throws
   ``ClassCastException: JsonSchema cannot be cast to ComposedSchema``
   in ``AbstractDartCodegen.fromProperty`` while processing paths. 3.1's
   ``anyOf: [{type: X}, {type: null}]`` nullability idiom is all over this
   spec, and the Dart generator predates it.

2. **It generates the wrong thing.** The snapshot carries 194 schemas across
   108 paths -- the entire staff surface. Emitting all of them into the member
   app puts ``MemberDetailOut``, ``BranchMemberRosterOut`` and every staff
   shape one import away from a member screen. The member client should be
   unable to NAME a staff shape, which is the same principle as the
   ``/member`` path guard (FM-H).

So this generator walks only the ``/member/*`` paths and the schemas
transitively reachable from them. It is deterministic, has no Java, Docker or
network dependency, and runs in the same seconds-long step on both pipelines.

The output is a GENERATED file and is never hand-edited -- the same hard rule
that governs ``web/packages/api-client/src/generated/schema.d.ts``.
``mobile:codegen-drift`` runs this with ``--check`` and fails on any diff.

Usage:
    python mobile/scripts/generate_api_models.py            # write
    python mobile/scripts/generate_api_models.py --check    # fail on drift
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "web" / "packages" / "api-client" / "openapi.json"
OUTPUT = (
    REPO_ROOT / "mobile" / "gp_api_client" / "lib" / "src" / "generated" / "member_models.dart"
)
MEMBER_PREFIX = "/member/"

# Schemas the member app must never carry, even if a /member path happens to
# reference one. Nothing hits this today; it is a tripwire, so that a future
# backend change that leaks a staff shape into a member response fails the
# generator instead of quietly widening the client's vocabulary.
FORBIDDEN = {
    "MemberDetailOut",
    "MemberListDetailResponse",
    "MemberListResponse",
    "BranchMemberRosterOut",
    "BranchMemberRosterResponse",
}


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def collect_refs(node: Any, found: set[str]) -> None:
    """Every ``$ref`` reachable from ``node``."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.add(ref_name(ref))
        for value in node.values():
            collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            collect_refs(item, found)


def member_schemas(spec: dict[str, Any]) -> list[str]:
    """Schema names reachable from the /member surface, transitively."""
    schemas: dict[str, Any] = spec.get("components", {}).get("schemas", {})
    seed: set[str] = set()
    for path, operations in spec.get("paths", {}).items():
        if not path.startswith(MEMBER_PREFIX):
            continue
        collect_refs(operations, seed)

    # Transitive closure: a referenced schema's own references come too.
    reachable: set[str] = set()
    pending = list(seed)
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        if name not in schemas:
            die(f"/member surface references unknown schema {name!r}")
        reachable.add(name)
        nested: set[str] = set()
        collect_refs(schemas[name], nested)
        pending.extend(nested - reachable)

    leaked = sorted(reachable & FORBIDDEN)
    if leaked:
        die(
            "the /member surface now references staff-only schema(s) "
            f"{leaked}: a member response is disclosing a staff shape"
        )
    return sorted(reachable)


def unwrap_nullable(schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Resolve the OpenAPI 3.1 ``anyOf: [X, {type: null}]`` nullability idiom."""
    any_of = schema.get("anyOf")
    if not isinstance(any_of, list):
        return schema, False
    non_null = [s for s in any_of if s.get("type") != "null"]
    nullable = len(non_null) != len(any_of)
    if len(non_null) == 1:
        return non_null[0], nullable
    # A genuine union of two or more real types has no faithful Dart shape;
    # Object? is honest about that rather than picking one arbitrarily.
    return {}, True


def dart_type(schema: dict[str, Any], required: bool) -> str:
    schema, nullable = unwrap_nullable(schema)
    optional = nullable or not required

    ref = schema.get("$ref")
    if isinstance(ref, str):
        return ref_name(ref) + ("?" if optional else "")

    kind = schema.get("type")
    if kind == "string":
        base = "String"
    elif kind == "integer":
        base = "int"
    elif kind == "number":
        # Money is a STRING everywhere in this API (gate 1.1). A `number` here
        # is a rate or a count, never an amount.
        base = "double"
    elif kind == "boolean":
        base = "bool"
    elif kind == "array":
        base = f"List<{dart_type(schema.get('items', {}), required=True)}>"
    elif kind == "object" or "properties" in schema:
        base = "Map<String, Object?>"
    else:
        return "Object?"
    return base + ("?" if optional else "")


def dart_field_name(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def decode_expression(schema: dart_type.__annotations__["schema"], required: bool, key: str) -> str:  # type: ignore[index]
    """Dart source that reads ``key`` out of a ``Map<String, Object?> json``."""
    resolved, nullable = unwrap_nullable(schema)
    optional = nullable or not required
    raw = f"json['{key}']"

    ref = resolved.get("$ref")
    if isinstance(ref, str):
        cls = ref_name(ref)
        if optional:
            return (
                f"{raw} == null ? null : "
                f"{cls}.fromJson({raw}! as Map<String, Object?>)"
            )
        return f"{cls}.fromJson({raw}! as Map<String, Object?>)"

    if resolved.get("type") == "array":
        item = resolved.get("items", {})
        item_type = dart_type(item, required=True)
        item_ref = item.get("$ref")
        if isinstance(item_ref, str):
            element = f"{ref_name(item_ref)}.fromJson(e! as Map<String, Object?>)"
        else:
            element = f"e! as {item_type}"
        listing = (
            f"({raw}! as List<Object?>).map((Object? e) => {element}).toList(growable: false)"
        )
        return f"{raw} == null ? null : {listing}" if optional else listing

    target = dart_type(schema, required)
    return f"{raw} as {target}"


def encode_expression(schema: dict[str, Any], required: bool, field: str) -> str:
    resolved, nullable = unwrap_nullable(schema)
    optional = nullable or not required
    if isinstance(resolved.get("$ref"), str):
        return f"{field}?.toJson()" if optional else f"{field}.toJson()"
    if resolved.get("type") == "array":
        item = resolved.get("items", {})
        if isinstance(item.get("$ref"), str):
            mapped = f"{field}{'?' if optional else ''}.map((Object? e) => (e! as dynamic).toJson()).toList(growable: false)"
            return mapped
    return field


def render_class(name: str, schema: dict[str, Any]) -> str:
    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    lines: list[str] = []

    description = (schema.get("description") or "").strip().splitlines()
    if description:
        lines.append("/// " + description[0].strip())
        for extra in description[1:]:
            lines.append("/// " + extra.strip() if extra.strip() else "///")
    else:
        lines.append(f"/// `{name}` from the OpenAPI snapshot.")
    lines.append("@immutable")
    lines.append(f"class {name} {{")

    ordered = sorted(properties.items())
    if ordered:
        lines.append(f"  const {name}({{")
        for key, prop in ordered:
            field = dart_field_name(key)
            is_required = key in required and not unwrap_nullable(prop)[1]
            lines.append(f"    {'required ' if is_required else ''}this.{field},")
        lines.append("  });")
    else:
        lines.append(f"  const {name}();")
    lines.append("")

    for key, prop in ordered:
        field = dart_field_name(key)
        lines.append(f"  final {dart_type(prop, key in required)} {field};")
    if ordered:
        lines.append("")

    lines.append(f"  factory {name}.fromJson(Map<String, Object?> json) => {name}(")
    for key, prop in ordered:
        field = dart_field_name(key)
        lines.append(f"        {field}: {decode_expression(prop, key in required, key)},")
    lines.append("      );")
    lines.append("")

    lines.append("  Map<String, Object?> toJson() => <String, Object?>{")
    for key, prop in ordered:
        field = dart_field_name(key)
        lines.append(f"        '{key}': {encode_expression(prop, key in required, field)},")
    lines.append("      };")
    lines.append("}")
    return "\n".join(lines)


def render(spec: dict[str, Any]) -> str:
    schemas = spec["components"]["schemas"]
    names = member_schemas(spec)
    member_paths = sorted(p for p in spec.get("paths", {}) if p.startswith(MEMBER_PREFIX))

    header = [
        "// GENERATED FILE -- DO NOT EDIT.",
        "//",
        "// Produced by mobile/scripts/generate_api_models.py from",
        "// web/packages/api-client/openapi.json, the binding contract.",
        "// Run that script and commit the result; mobile:codegen-drift fails",
        "// the pipeline on any hand-edit or stale snapshot.",
        "//",
        "// Scope: the schemas reachable from the /member surface, and nothing",
        "// else. The member client cannot name a staff shape.",
        "//",
        "// Member paths in this snapshot:",
    ]
    header += [f"//   {path}" for path in member_paths]
    header += [
        "",
        "import 'package:meta/meta.dart';",
        "",
    ]

    body = [render_class(name, schemas[name]) for name in names]
    return "\n".join(header) + "\n\n".join(body) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed file drifts")
    args = parser.parse_args()

    if not SPEC.exists():
        die(f"OpenAPI snapshot not found at {SPEC}")
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    rendered = render(spec)

    if args.check:
        if not OUTPUT.exists():
            die(f"{OUTPUT.relative_to(REPO_ROOT)} is missing -- run the generator and commit it")
        current = OUTPUT.read_text(encoding="utf-8")
        if current != rendered:
            print(
                "error: the generated member models are stale or hand-edited.\n"
                "       Run: python mobile/scripts/generate_api_models.py\n"
                "       then commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT.relative_to(REPO_ROOT)} is current.")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
