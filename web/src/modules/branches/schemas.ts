import { z } from "zod";
import { isoTimestampSchema } from "@/lib/schemas";
import { memberStatusSchema } from "@/modules/members/schemas";
import { USER_STATUSES } from "@/modules/users/schemas";

/**
 * Zod-validated response boundary for the branches registry console
 * (the registry contract; risk 1 scope: production API with no console surface). Shapes mirror the
 * generated client types (components["schemas"]["BranchOut"],
 * BranchAssignmentOut, BackfillRunOut) — the drift-checked OpenAPI
 * snapshot remains the contract; these schemas only assert it at
 * runtime.
 *
 * NO MONEY FIELD exists on this contract (honesty over decoration): a
 * branch is organisational metadata only — the router documents that
 * "no money/cost/path parameter exists here for a caller to supply,
 * and none may ride along" (least disclosure). The shared moneySchema is
 * deliberately NOT imported; asserting a money shape the contract
 * does not carry would be fake validation.
 *
 * Field-by-field shape derivation (backend/src/genesis, not guessed):
 * - created_at: datetime.isoformat() (api/branches.py:_out) →
 *   `isoTimestampSchema` — feeds fmtDateTime; garbage is REJECTED at
 *   the boundary, never rendered "Invalid Date" (posture).
 * - name: free text (DB CHECK btrim(name) <> '', migration 0016) —
 *   rendered exclusively as React text (XSS-inert, gate-tested).
 * - version: optimistic-lock integer (concurrency safety) pinning every rename;
 *   BranchAssignmentOut.version is the USER/MEMBER row's new version
 *   (the assignment locks the entity being edited, not the branch).
 * - BackfillRunOut: side-effect COUNTS only — integers, nothing
 *   else; a completed re-run legitimately reports all zeros.
 * - No status/enum vocabulary exists anywhere on this contract.
 */
export const branchSchema = z.object({
  id: z.string(),
  name: z.string(),
  version: z.number().int(),
  created_at: isoTimestampSchema,
});

export type BranchRecord = z.infer<typeof branchSchema>;

/**
 * Assignment result (BranchAssignmentOut): the server's fact that the
 * USER/MEMBER row now points at the branch — entity id, branch id +
 * name, and the entity row's NEW optimistic-lock version.
 */
export const branchAssignmentSchema = z.object({
  entity_id: z.string(),
  branch_id: z.string(),
  branch_name: z.string(),
  version: z.number().int(),
});

export type BranchAssignment = z.infer<typeof branchAssignmentSchema>;

/** Backfill run report (BackfillRunOut): what the run created and
 * linked — a completed re-run scans nothing (lock-free no-op, proven
 * by these counts, never by prose). */
export const backfillRunSchema = z.object({
  scanned: z.number().int(),
  branches_created: z.number().int(),
  users_linked: z.number().int(),
  batches: z.number().int(),
});

export type BackfillRun = z.infer<typeof backfillRunSchema>;

/* ------------------------------------------------------------------ *
 * Branch rosters — the expand-only reads over the
 * 0016 assignment columns, mirroring the assignment
 * permission split on READS: the user roster is access_control:view,
 * the member roster is members:view — never a settings right.
 * Identity facts ONLY (least disclosure, least disclosure): no role, no
 * last-active, no contact details, NO money field — none is asserted
 * or invented (the fake-validation posture). Status
 * vocabularies are the OWNING modules' pinned enums (reuse-first, reuse-first): an unknown status is a contract violation, REJECTED.
 * ------------------------------------------------------------------ */
export const branchUserRosterSchema = z.object({
  id: z.string(),
  /** Operator free text — rendered exclusively as inert React text
   * (the module's named XSS threat). */
  full_name: z.string(),
  email: z.string(),
  status: z.enum(USER_STATUSES),
});

export type BranchUserRosterRow = z.infer<typeof branchUserRosterSchema>;

export const branchMemberRosterSchema = z.object({
  id: z.string(),
  member_no: z.string(),
  /** Operator free text — inert React text only. */
  name: z.string(),
  status: memberStatusSchema,
});

export type BranchMemberRosterRow = z.infer<typeof branchMemberRosterSchema>;

/* ------------------------------------------------------------------ *
 * Form-side entry shape (client Zod message only — the server's
 * str_strip_whitespace + min/max and the duplicate-name 409 are its
 * verdicts, rendered, never pre-empted beyond shape). Contract bounds
 * (BranchCreateBody/BranchUpdateBody): 1..120 after strip.
 * ------------------------------------------------------------------ */
export const branchNameEntrySchema = z.object({
  name: z.string().min(1, "Enter a branch name.").max(120, "At most 120 characters."),
});

export type BranchNameEntry = z.infer<typeof branchNameEntrySchema>;
