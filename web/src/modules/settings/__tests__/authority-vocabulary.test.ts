/**
 * Authority-vocabulary drift tripwire (W57-5).
 *
 * `AUTHORITY_ROLE_NAMES` in settings/schemas.ts mirrors the backend's
 * code-owned platform role names BY HAND (UX convenience for the
 * approval-matrix picker; the server validates the vocabulary
 * regardless — an unknown authority is a 422). The generated OpenAPI
 * contract does NOT expose the role names as an enum (ApprovalBand
 * authority is a plain string), so a contract-level pin is impossible;
 * this test pins the mirror to the backend SOURCE OF TRUTH instead:
 * `backend/src/genesis/domain/rbac.py` ROLE_NAMES (immutable by DB
 * trigger). If the backend vocabulary changes, this test fails and the
 * picker is updated in the same change — the silent-divergence window
 * (picker offering names the server 422s) is closed.
 *
 * Falsifiable: reorder, add, remove or rename an entry on EITHER side
 * and the exact-match assertion fails; mangle the parser's anchors in
 * rbac.py and the structural preconditions fail loudly rather than
 * passing vacuously.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { AUTHORITY_ROLE_NAMES } from "../schemas";

const RBAC_SOURCE = join(
  __dirname,
  "..", "..", "..", "..", "..",
  "backend", "src", "genesis", "domain", "rbac.py",
);

/** Parse the ROLE_NAMES tuple from the backend source: resolve each
 * constant identifier in the tuple to its string literal. */
function backendRoleNames(): string[] {
  const source = readFileSync(RBAC_SOURCE, "utf8");

  const tuple = /ROLE_NAMES: tuple\[str, \.\.\.\] = \(([^)]*)\)/.exec(source);
  if (tuple === null || tuple[1] === undefined) {
    throw new Error("ROLE_NAMES tuple not found in rbac.py — parser anchor drifted");
  }
  const identifiers = tuple[1]
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
  if (identifiers.length === 0) {
    throw new Error("ROLE_NAMES tuple parsed empty — parser anchor drifted");
  }

  return identifiers.map((identifier) => {
    const constant = new RegExp(`^${identifier} = "([^"]+)"$`, "m").exec(source);
    if (constant === null || constant[1] === undefined) {
      throw new Error(`role constant ${identifier} not found in rbac.py`);
    }
    return constant[1];
  });
}

describe("AUTHORITY_ROLE_NAMES drift tripwire (W57-5)", () => {
  it("parses a non-trivial backend vocabulary (the tripwire cannot pass vacuously)", () => {
    expect(backendRoleNames().length).toBeGreaterThanOrEqual(5);
  });

  it("mirrors backend ROLE_NAMES exactly (order and content)", () => {
    expect([...AUTHORITY_ROLE_NAMES]).toEqual(backendRoleNames());
  });
});
