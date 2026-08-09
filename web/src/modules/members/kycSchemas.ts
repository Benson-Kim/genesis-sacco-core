import { z } from "zod";
import { isoTimestampSchema } from "@/lib/schemas";
import { memberTypeSchema, type MemberType } from "./schemas";

/**
 * Member-KYC boundary shapes (the profile and document-checklist contracts).
 *
 * SINGLE SOURCE OF TRUTH, MIRRORED HONESTLY: the per-type section and
 * field specs below reproduce `backend/src/genesis/domain/member_kyc.py`
 * field-for-field (PersonProfile, CompanyProfile, GroupProfile,
 * VehicleProfile — pydantic models with extra="forbid", DB-CHECKed by
 * migration 0018), with the prototype's wizard labels
 * (`genesis_prestige_app.html` `forms` map). The Zod profile schemas
 * are BUILT from these specs, so the wizard form, the profile detail
 * rendering and the response boundary can never drift from each other;
 * their fidelity to the backend is pinned by hand-computed
 * accept/reject oracles in the test suites.
 *
 * MONEY POSTURE: the ONE monetary-looking KYC field
 * (monthly income) is an INFORMATIONAL string constrained to the
 * backend's decimal grammar (`_AMOUNT_PATTERN`, up to two decimal
 * places). It is never an input to any computation and never rendered
 * through fmtKes: it is not ledger money, it is declared KYC data,
 * shown verbatim.
 *
 * PII POSTURE (least disclosure): every profile field is PII. Values render
 * exclusively through React text interpolation on members:view-gated
 * surfaces; nothing here is logged, echoed into errors, or placed in
 * a URL.
 */

/** Backend `_AMOUNT_PATTERN`: informational decimal grammar, never float. */
export const KYC_AMOUNT_RE = /^\d{1,16}(\.\d{1,2})?$/;

/** `date.isoformat()` (calendar DATE, not a timestamp). */
export const KYC_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export type KycFieldKind = "text" | "email" | "date" | "int" | "amount";

export interface KycFieldSpec {
  /** Wire key inside the section object (backend field name). */
  readonly key: string;
  /** Operator-facing label (prototype vocabulary). */
  readonly label: string;
  readonly kind: KycFieldKind;
  /** Optional fields are nullable on the wire (pydantic `| None`). */
  readonly required: boolean;
  /** Text/email max length (mirrors the pydantic Field constraint). */
  readonly maxLength?: number;
  /** Integer bounds (mirrors the pydantic ge/le constraints). */
  readonly intMin?: number;
  readonly intMax?: number;
}

export interface KycSectionSpec {
  /**
   * Path of the section object inside the profile payload — one key
   * for top-level sections, two for the nested group officials
   * (`officials.chairperson` etc.).
   */
  readonly path: readonly string[];
  /** Section heading (prototype wizard vocabulary). */
  readonly label: string;
  readonly fields: readonly KycFieldSpec[];
  /**
   * Set ONLY on the company signatories section: the wire shape is a
   * LIST of section objects (1..10 server-side); the wizard captures
   * the prototype's single signatory section and submits a one-item
   * list — additional signatories are a profile-edit concern.
   */
  readonly repeatsAsList?: boolean;
}

function text(key: string, label: string, maxLength: number): KycFieldSpec {
  return { key, label, kind: "text", required: true, maxLength };
}

function optionalField(spec: KycFieldSpec): KycFieldSpec {
  return { ...spec, required: false };
}

function date(key: string, label: string): KycFieldSpec {
  return { key, label, kind: "date", required: true };
}

function int(key: string, label: string, intMin: number, intMax: number): KycFieldSpec {
  return { key, label, kind: "int", required: true, intMin, intMax };
}

/** Per-type wizard sections (backend domain/member_kyc.py, verbatim). */
export const KYC_FORM_SECTIONS: Record<MemberType, readonly KycSectionSpec[]> = {
  person: [
    {
      path: ["bio"],
      label: "Bio data",
      fields: [
        text("first_name", "First name", 100),
        text("surname", "Surname", 100),
        text("gender", "Gender", 20),
        date("date_of_birth", "Date of birth"),
        text("id_number", "ID number", 20),
        text("kra_pin", "KRA PIN", 20),
      ],
    },
    {
      path: ["contact"],
      label: "Contact",
      fields: [
        text("phone", "Phone (M-Pesa)", 32),
        optionalField({ key: "email", label: "Email", kind: "email", required: true, maxLength: 254 }),
        text("county", "County", 60),
        text("physical_address", "Physical address", 200),
      ],
    },
    {
      path: ["employment"],
      label: "Employment",
      fields: [
        text("employment_status", "Employment status", 60),
        text("occupation", "Occupation", 100),
        { key: "monthly_income", label: "Monthly income (KES)", kind: "amount", required: true },
      ],
    },
    {
      path: ["next_of_kin"],
      label: "Next of kin",
      fields: [
        text("name", "Name", 200),
        text("relationship", "Relationship", 60),
        text("phone", "Phone", 32),
      ],
    },
  ],
  company: [
    {
      path: ["registration"],
      label: "Company",
      fields: [
        text("registered_name", "Registered name", 200),
        text("reg_number", "Reg number", 60),
        text("kra_pin", "KRA PIN", 20),
        date("incorporated_on", "Incorporated on"),
        text("industry", "Industry", 100),
        text("nature_of_business", "Nature of business", 200),
      ],
    },
    {
      path: ["office"],
      label: "Office",
      fields: [text("address", "Address", 200), text("town", "Town", 60), text("county", "County", 60)],
    },
    {
      path: ["contact"],
      label: "Contact",
      fields: [
        text("contact_name", "Contact name", 200),
        text("role", "Role", 60),
        text("phone", "Phone", 32),
        optionalField({ key: "email", label: "Email", kind: "email", required: true, maxLength: 254 }),
      ],
    },
    {
      path: ["signatories"],
      label: "Signatory",
      repeatsAsList: true,
      fields: [
        text("director_name", "Director name", 200),
        text("id_number", "ID number", 20),
        text("role", "Role", 60),
      ],
    },
  ],
  group: [
    {
      path: ["registration"],
      label: "Group",
      fields: [
        text("group_name", "Group name", 200),
        text("group_type", "Group type", 60),
        text("reg_number", "Reg number", 60),
        date("date_formed", "Date formed"),
        text("county", "County", 60),
        int("members_count", "Members count", 1, 100_000),
      ],
    },
    {
      path: ["officials", "chairperson"],
      label: "Chairperson",
      fields: [text("name", "Name", 200), text("id_number", "ID number", 20), text("phone", "Phone", 32)],
    },
    {
      path: ["officials", "secretary"],
      label: "Secretary",
      fields: [text("name", "Name", 200), text("id_number", "ID number", 20), text("phone", "Phone", 32)],
    },
    {
      path: ["officials", "treasurer"],
      label: "Treasurer",
      fields: [text("name", "Name", 200), text("id_number", "ID number", 20), text("phone", "Phone", 32)],
    },
  ],
  vehicle: [
    {
      path: ["vehicle"],
      label: "Vehicle",
      fields: [
        text("registration_number", "Registration number", 20),
        text("make", "Make", 60),
        text("model", "Model", 60),
        int("year", "Year", 1950, 2100),
        text("body_type", "Body type", 60),
        int("passenger_capacity", "Passenger capacity", 1, 200),
      ],
    },
    {
      path: ["compliance"],
      label: "Compliance",
      fields: [
        text("route", "Route / SACCO route", 100),
        text("tlb_psv_licence", "TLB / PSV licence", 60),
        date("ntsa_inspection_expiry", "NTSA inspection expiry"),
        text("insurance_provider", "Insurance provider", 100),
        date("insurance_expiry", "Insurance expiry"),
      ],
    },
    {
      path: ["ownership"],
      label: "Ownership",
      fields: [
        text("registered_owner", "Registered owner", 200),
        text("ownership_type", "Ownership type", 60),
        optionalField(text("financier", "Financier", 200)),
      ],
    },
  ],
};

function fieldSchema(spec: KycFieldSpec): z.ZodTypeAny {
  let schema: z.ZodTypeAny;
  switch (spec.kind) {
    case "text":
      schema = z.string().min(1).max(spec.maxLength ?? 200);
      break;
    case "email":
      schema = z.string().email().max(spec.maxLength ?? 254);
      break;
    case "date":
      schema = z.string().regex(KYC_DATE_RE, "Use the YYYY-MM-DD date form");
      break;
    case "int":
      schema = z
        .number()
        .int()
        .min(spec.intMin ?? 0)
        .max(spec.intMax ?? Number.MAX_SAFE_INTEGER);
      break;
    case "amount":
      schema = z
        .string()
        .regex(KYC_AMOUNT_RE, "Digits with up to two decimal places");
      break;
  }
  // Optional pydantic fields serialise as EXPLICIT null in
  // model_dump(mode="json") — nullable, not undefined-optional, on the
  // read side; the form layer sends null for a blank optional input.
  return spec.required ? schema : schema.nullable();
}

function sectionSchema(fields: readonly KycFieldSpec[]): z.ZodTypeAny {
  const shape: Record<string, z.ZodTypeAny> = {};
  for (const field of fields) shape[field.key] = fieldSchema(field);
  return z.object(shape);
}

function profileSchemaFor(type: MemberType): z.ZodTypeAny {
  const shape: Record<string, z.ZodTypeAny> = {};
  const nested: Record<string, Record<string, z.ZodTypeAny>> = {};
  for (const section of KYC_FORM_SECTIONS[type]) {
    const built = sectionSchema(section.fields);
    if (section.repeatsAsList === true) {
      // Company signatories: 1..10 server-side (pydantic Field bounds).
      shape[section.path[0]!] = z.array(built).min(1).max(10);
    } else if (section.path.length === 2) {
      (nested[section.path[0]!] ??= {})[section.path[1]!] = built;
    } else {
      shape[section.path[0]!] = built;
    }
  }
  for (const [key, inner] of Object.entries(nested)) {
    shape[key] = z.object(inner);
  }
  return z.object(shape);
}

/**
 * Per-type profile payload schemas, BUILT from the specs above (one
 * source of truth). Used for client pre-validation of the wizard form
 * (the server re-validates and is the enforcer) AND for the response
 * boundary: a profile payload that does not match its member type's
 * schema is a contract violation and is REJECTED, never rendered.
 */
export const PROFILE_SCHEMAS: Record<MemberType, z.ZodTypeAny> = {
  person: profileSchemaFor("person"),
  company: profileSchemaFor("company"),
  group: profileSchemaFor("group"),
  vehicle: profileSchemaFor("vehicle"),
};

/**
 * ProfileOut boundary. `profile` is an opaque object on the OpenAPI
 * contract; this client validates it against the member type's
 * code-owned schema (mirroring the server's own validation) so a
 * wrong-type or malformed payload never reaches a screen.
 */
export const kycProfileSchema = z
  .object({
    id: z.string(),
    member_id: z.string(),
    member_type: memberTypeSchema,
    category: z.string(),
    profile: z.record(z.string(), z.unknown()),
    dpa_consent_at: isoTimestampSchema.nullable(),
    version: z.number().int(),
    created_at: isoTimestampSchema,
  })
  .superRefine((value, ctx) => {
    const verdict = PROFILE_SCHEMAS[value.member_type].safeParse(value.profile);
    if (!verdict.success) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["profile"],
        message: `profile payload does not match the ${value.member_type} schema`,
      });
    }
  });

export type KycProfile = z.infer<typeof kycProfileSchema>;

/** Client pre-validation of the wizard's non-profile inputs. */
export const kycProfileCreateSchema = z.object({
  category: z.string().min(1).max(60),
  dpa_consent: z.boolean(),
});

// ---------------------------------------------------------------------------
// Documents (checklist metadata; binary upload has NO contract — ADR-0003)
// ---------------------------------------------------------------------------

export const DOCUMENT_STATUSES = ["pending", "received", "verified", "rejected"] as const;
export const documentStatusSchema = z.enum(DOCUMENT_STATUSES);
export type DocumentStatus = z.infer<typeof documentStatusSchema>;

export const DOCUMENT_STATUS_LABELS: Record<DocumentStatus, string> = {
  pending: "Pending",
  received: "Received",
  verified: "Verified",
  rejected: "Rejected",
};

/**
 * Legal status moves, mirroring the backend status machine
 * (domain/member_kyc.py `_DOC_ALLOWED`): pending to received
 * (handover), received to verified or rejected (review), rejected to
 * received (resubmission); verified is terminal. Rendering vocabulary
 * ONLY — the server validates every transition regardless (concurrency safety).
 */
export const DOCUMENT_TRANSITIONS: Record<DocumentStatus, readonly DocumentStatus[]> = {
  pending: ["received"],
  received: ["verified", "rejected"],
  rejected: ["received"],
  verified: [],
};

export const DOCUMENT_ACTION_LABELS: Record<DocumentStatus, string> = {
  received: "Mark received",
  verified: "Verify",
  rejected: "Reject",
  pending: "Reset", // never offered: no transition targets pending
};

/**
 * Per-type required-documents checklist, mirroring the backend
 * DOCUMENT_CHECKLISTS (code-owned, DB-CHECKed by 0018) with the
 * prototype's `docsFor` labels. A doc type outside the member type's
 * checklist is a server 422 — the UI only ever offers these.
 */
export const KYC_DOC_TYPES: Record<MemberType, readonly string[]> = {
  person: ["national_id_copy", "kra_pin", "passport_photo", "signature", "next_of_kin_id"],
  company: ["cert_of_incorporation", "cr12", "kra_pin", "board_resolution", "signatory_ids"],
  group: ["registration_cert", "constitution", "members_list", "resolution", "officials_ids"],
  vehicle: ["logbook", "insurance_cert", "ntsa_inspection", "psv_tlb_licence", "sacco_agreement"],
};

export const KYC_DOC_LABELS: Record<string, string> = {
  national_id_copy: "National ID copy",
  kra_pin: "KRA PIN",
  passport_photo: "Passport photo",
  signature: "Signature",
  next_of_kin_id: "Next-of-kin ID",
  cert_of_incorporation: "Cert of incorporation",
  cr12: "CR12",
  board_resolution: "Board resolution",
  signatory_ids: "Signatory IDs",
  registration_cert: "Registration cert",
  constitution: "Constitution",
  members_list: "Members list",
  resolution: "Resolution",
  officials_ids: "Officials IDs",
  logbook: "Logbook",
  insurance_cert: "Insurance cert",
  ntsa_inspection: "NTSA inspection",
  psv_tlb_licence: "PSV/TLB licence",
  sacco_agreement: "SACCO agreement",
};

/** Operator-facing label for a doc type; unknown types (possible on a
 * widened future contract) render their wire key verbatim as TEXT. */
export function docTypeLabel(docType: string): string {
  return KYC_DOC_LABELS[docType] ?? docType;
}

export const kycDocumentSchema = z.object({
  id: z.string(),
  member_id: z.string(),
  doc_type: z.string(),
  status: documentStatusSchema,
  expires_at: z.string().regex(KYC_DATE_RE).nullable(),
  version: z.number().int(),
  created_at: isoTimestampSchema,
});

export type KycDocument = z.infer<typeof kycDocumentSchema>;
