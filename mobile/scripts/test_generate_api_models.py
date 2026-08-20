#!/usr/bin/env python3
"""Guards on the model generator. Each fails when its guard is removed.

Run: python mobile/scripts/test_generate_api_models.py

Plain stdlib, no pytest, because this runs inside the mobile pipeline next to
the drift check rather than in the backend's Python job.
"""

from __future__ import annotations

import sys

import generate_api_models as gen


FAILURES: list[str] = []


def check(condition: bool, description: str) -> None:
    if condition:
        print(f"  ok   {description}")
    else:
        print(f"  FAIL {description}")
        FAILURES.append(description)


def expect_exit(fn, description: str) -> None:
    try:
        fn()
    except SystemExit:
        print(f"  ok   {description}")
        return
    print(f"  FAIL {description}")
    FAILURES.append(description)


def spec_with(paths: dict, schemas: dict) -> dict:
    return {"paths": paths, "components": {"schemas": schemas}}


def main() -> int:
    print("scope selection")
    spec = spec_with(
        paths={
            "/member/me": {"get": {"responses": {"200": {"$ref": "#/components/schemas/Mine"}}}},
            "/members/{id}": {
                "get": {"responses": {"200": {"$ref": "#/components/schemas/StaffOnly"}}}
            },
        },
        schemas={
            "Mine": {"type": "object", "properties": {}},
            "StaffOnly": {"type": "object", "properties": {}},
            "Unreferenced": {"type": "object", "properties": {}},
        },
    )
    selected = gen.member_schemas(spec)
    check(selected == ["Mine"], "only /member-reachable schemas are selected")
    check("StaffOnly" not in selected, "a /members path does NOT pull in its schema")
    check(
        "Unreferenced" not in selected,
        "an unreferenced schema is not emitted just for existing",
    )

    print("transitive closure")
    spec = spec_with(
        paths={
            "/member/me": {"get": {"responses": {"200": {"$ref": "#/components/schemas/Outer"}}}}
        },
        schemas={
            "Outer": {
                "type": "object",
                "properties": {"inner": {"$ref": "#/components/schemas/Inner"}},
            },
            "Inner": {
                "type": "object",
                "properties": {"deep": {"$ref": "#/components/schemas/Deep"}},
            },
            "Deep": {"type": "object", "properties": {}},
        },
    )
    check(
        gen.member_schemas(spec) == ["Deep", "Inner", "Outer"],
        "nested references are followed to the bottom",
    )

    print("tripwires")
    leaky = spec_with(
        paths={
            "/member/me": {
                "get": {"responses": {"200": {"$ref": "#/components/schemas/MemberDetailOut"}}}
            }
        },
        schemas={"MemberDetailOut": {"type": "object", "properties": {}}},
    )
    expect_exit(
        lambda: gen.member_schemas(leaky),
        "a staff-only schema reachable from /member is refused, not emitted",
    )

    dangling = spec_with(
        paths={
            "/member/me": {"get": {"responses": {"200": {"$ref": "#/components/schemas/Ghost"}}}}
        },
        schemas={},
    )
    expect_exit(
        lambda: gen.member_schemas(dangling),
        "a dangling $ref is refused rather than silently skipped",
    )

    print("3.1 nullability")
    nullable = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    check(
        gen.dart_type(nullable, required=True) == "String?",
        "anyOf[X, null] becomes a nullable Dart type",
    )
    check(
        gen.dart_type({"type": "string"}, required=True) == "String",
        "a required non-null string is non-nullable",
    )
    check(
        gen.dart_type({"type": "string"}, required=False) == "String?",
        "an optional field is nullable even when its type is not",
    )
    check(
        gen.dart_type({"anyOf": [{"type": "string"}, {"type": "integer"}]}, required=True)
        == "Object?",
        "a genuine multi-type union degrades to Object? rather than guessing",
    )

    print("money stays a string")
    money = {"type": "string"}
    check(
        gen.dart_type(money, required=True) == "String",
        "an amount is a String, never a double (gate 1.1)",
    )

    print("determinism")
    real = gen.json.loads(gen.SPEC.read_text(encoding="utf-8"))
    check(gen.render(real) == gen.render(real), "rendering twice gives identical bytes")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} generator guard(s) failed.", file=sys.stderr)
        return 1
    print("All generator guards passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
