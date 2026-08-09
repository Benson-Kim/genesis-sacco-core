import { z } from "zod";

/**
 * Zod validation for the members module (P8 API). Enums mirror the
 * backend MemberStatus/MemberType; an unknown value is a contract
 * violation and is REJECTED, never silently rendered.
 */
export const MEMBER_STATUSES = ["active", "arrears", "dormant", "exited"] as const;
export const MEMBER_TYPES = ["person", "company", "group", "vehicle"] as const;

export const memberStatusSchema = z.enum(MEMBER_STATUSES);
export const memberTypeSchema = z.enum(MEMBER_TYPES);

export type MemberStatus = z.infer<typeof memberStatusSchema>;
export type MemberType = z.infer<typeof memberTypeSchema>;

export const memberSchema = z.object({
    id: z.string(),
    member_no: z.string(),
    type: memberTypeSchema,
    name: z.string(),
    phone: z.string().nullable(),
    email: z.string().nullable(),
    status: memberStatusSchema,
    version: z.number().int(),
    /** Branch attribution — the 0016 FK written
     * ONLY by the assignment route, as the bare branch UUID.
     * NULLABLE, NOT optional: every member read carries the key, so a
     * response missing it is a contract violation and is REJECTED
     * (network-tested). NULL renders the honest "unassigned"
     * affordance — attribution is never invented. Resolving the
     * branch NAME stays behind settings:view (GET /branches/{id});
     * without that grant the drawer renders the short id only and
     * fetches nothing (the created_by least-disclosure precedent). */
    branch_id: z.string().nullable(),
    /** Dividend payout PREFERENCE — the authorized
     *  expand. NULLABLE, NOT optional: every member read
     * carries the key, so a response missing it is a contract
     * violation and is REJECTED (network-tested). NULL renders the
     * honest "not set" affordance — a preference is never invented.
     * The value is the server's CODE-OWNED vocabulary token rendered
     * VERBATIM (the backend deliberately types it as a bounded string
     * and never echoes the vocabulary — least disclosure); the client
     * pins no duplicate enum. Stored preference ONLY: the
     * distribution engine does not consume it (fence). */
    dividend_payout: z.string().nullable(),
});

export type Member = z.infer<typeof memberSchema>;

/**
 * Advisory financial aggregates on the single-member DETAIL read
 *  and on register LIST rows fetched with the OPT-IN
 * include=aggregates expand (review). All four figures
 * are decimal STRINGS rendered verbatim: NO client-side money math
 * ever, no summing, no derived figures. Required,
 * not optional, per the contract: the detail read and every opted-in
 * list row always carry the object, so a missing or null aggregates
 * is a contract violation and is REJECTED.
 */
export const memberAggregatesSchema = z.object({
    deposits_total: z.string(),
    shares_total: z.string(),
    loans_outstanding: z.string(),
    guarantees_pledged: z.string(),
});

export type MemberAggregates = z.infer<typeof memberAggregatesSchema>;

export const memberDetailSchema = memberSchema.extend({
    aggregates: memberAggregatesSchema,
});

export type MemberDetail = z.infer<typeof memberDetailSchema>;

/**
 * Dormancy run report (DormancyRunOut): SERVER facts
 * only. as_of/cutoff are DATE isoformat strings ("YYYY-MM-DD",
 * api/members.py date.isoformat()) rendered VERBATIM — deliberately
 * NOT isoTimestampSchema, which would reject every legitimate
 * response (the InterestRunOut precedent). Counts are integers; NO
 * money field exists on this contract and none is asserted or
 * invented (fake validation is a rejected posture). The dormancy
 * period itself resolves exclusively from tenant settings server-side
 * (period_months is the server's echo, never an input).
 */
export const dormancyRunSchema = z.object({
    as_of: z.string(),
    cutoff: z.string(),
    period_months: z.number().int(),
    scanned: z.number().int(),
    transitioned: z.number().int(),
    batches: z.number().int(),
});

export type DormancyRun = z.infer<typeof dormancyRunSchema>;

/** Client-side pre-validation of the create form (server re-validates). */
export const memberCreateSchema = z.object({
    type: memberTypeSchema,
    name: z.string().min(1).max(200),
    phone: z.string().max(32).nullable(),
    email: z.string().email().max(254).nullable(),
});

export type MemberCreateInput = z.infer<typeof memberCreateSchema>;

export const STATUS_LABELS: Record<MemberStatus, string> = {
    active: "Active",
    arrears: "In arrears",
    dormant: "Dormant",
    exited: "Exited",
};

export const TYPE_LABELS: Record<MemberType, string> = {
    person: "Person",
    company: "Company",
    group: "Group",
    vehicle: "Vehicle",
};
