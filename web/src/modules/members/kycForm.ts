/**
 * KYC wizard/edit form assembly: flat form values
 * to the nested wire payload and back, driven ENTIRELY by the specs in
 * kycSchemas.ts (one source of truth — the form cannot drift from the
 * boundary schema).
 *
 * Flat keys are the spec path joined with the field key
 * (`bio.first_name`, `officials.chairperson.name`,
 * `signatories.director_name` — the wizard captures the prototype's
 * single signatory section and the payload carries a one-item list).
 *
 * NO computation happens on any value: strings are trimmed and placed
 * verbatim; integer fields (member count, vehicle year, passenger
 * capacity — none of them money) are converted with Number() only when
 * they are all digits; the informational monthly-income string stays a
 * STRING end-to-end (the money rule posture in kycSchemas.ts).
 */
import type { ZodError } from "zod";
import type { FieldErrors } from "@/modules/forms/form-errors";
import {
  KYC_FORM_SECTIONS,
  PROFILE_SCHEMAS,
  type KycFieldSpec,
  type KycSectionSpec,
} from "./kycSchemas";
import type { MemberType } from "./schemas";

export function kycFieldKey(path: readonly string[], field: string): string {
  return [...path, field].join(".");
}

function wireValue(spec: KycFieldSpec, raw: string): unknown {
  const trimmed = raw.trim();
  if (trimmed === "") {
    // Optional fields travel as EXPLICIT null; a blank required field
    // becomes undefined so the Zod schema names it as missing.
    return spec.required ? undefined : null;
  }
  if (spec.kind === "int") {
    return /^\d+$/.test(trimmed) ? Number(trimmed) : trimmed;
  }
  return trimmed;
}

/** Assemble the nested wire payload from flat form values. */
export function buildProfilePayload(
  type: MemberType,
  values: Readonly<Record<string, string>>,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const section of KYC_FORM_SECTIONS[type]) {
    const sectionObject: Record<string, unknown> = {};
    for (const field of section.fields) {
      const value = wireValue(field, values[kycFieldKey(section.path, field.key)] ?? "");
      if (value !== undefined) sectionObject[field.key] = value;
    }
    if (section.repeatsAsList === true) {
      payload[section.path[0]!] = [sectionObject];
    } else if (section.path.length === 2) {
      const outer = (payload[section.path[0]!] ??= {}) as Record<string, unknown>;
      outer[section.path[1]!] = sectionObject;
    } else {
      payload[section.path[0]!] = sectionObject;
    }
  }
  return payload;
}

/**
 * Client pre-validation against the type's built schema (the server
 * re-validates and is the enforcer). Returns per-field messages keyed
 * by the FLAT form key; list indices are dropped from the key (the
 * form renders one signatory section).
 */
export function validateProfilePayload(
  type: MemberType,
  payload: Record<string, unknown>,
): FieldErrors {
  const verdict = PROFILE_SCHEMAS[type].safeParse(payload);
  if (verdict.success) return {};
  const out: Record<string, string> = {};
  for (const issue of (verdict.error as ZodError).issues) {
    const key = issue.path.filter((part) => typeof part === "string").join(".");
    if (!(key in out)) out[key] = issue.message;
  }
  return out;
}

/**
 * Extract one section's object from a SERVER profile payload (one copy for the edit-form flattening AND the read-only rendering — reuse-first). A one-item signatory list maps back to the single
 * section; anything absent reads as an empty object.
 */
export function sectionData(
  profile: Readonly<Record<string, unknown>>,
  section: KycSectionSpec,
): Record<string, unknown> {
  let sectionObject: unknown;
  if (section.repeatsAsList === true) {
    const list = profile[section.path[0]!];
    sectionObject = Array.isArray(list) ? list[0] : undefined;
  } else if (section.path.length === 2) {
    const outer = profile[section.path[0]!];
    sectionObject =
      typeof outer === "object" && outer !== null
        ? (outer as Record<string, unknown>)[section.path[1]!]
        : undefined;
  } else {
    sectionObject = profile[section.path[0]!];
  }
  return typeof sectionObject === "object" && sectionObject !== null
    ? (sectionObject as Record<string, unknown>)
    : {};
}

/**
 * Flatten a SERVER profile payload into form values for the edit form
 * (values render/edit verbatim; anything absent renders blank).
 */
export function profileToFormValues(
  type: MemberType,
  profile: Readonly<Record<string, unknown>>,
): Record<string, string> {
  const values: Record<string, string> = {};
  for (const section of KYC_FORM_SECTIONS[type]) {
    const record = sectionData(profile, section);
    for (const field of section.fields) {
      const raw = record[field.key];
      values[kycFieldKey(section.path, field.key)] =
        raw === null || raw === undefined ? "" : String(raw);
    }
  }
  return values;
}
