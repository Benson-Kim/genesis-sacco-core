"use client";

/**
 * Per-type KYC section form fields, rendered from
 * the specs in kycSchemas.ts — one copy shared by the registration
 * wizard and the drawer's edit form (reuse-first). Every field goes
 * through the shared FormField primitive (persistent label, aria-describedby/aria-invalid, inline client+server errors — blocker (f)).
 */
import { FormField } from "@/modules/forms/FormField";
import type { FieldErrors } from "@/modules/forms/form-errors";
import { KYC_FORM_SECTIONS, type KycFieldSpec } from "../kycSchemas";
import { kycFieldKey } from "../kycForm";
import type { MemberType } from "../schemas";
import styles from "./Members.module.css";

function inputProps(spec: KycFieldSpec): Record<string, unknown> {
  switch (spec.kind) {
    case "date":
      return { type: "date" };
    case "email":
      //  item 12 inputmode sweep: email keyboard on touch devices.
      return { type: "email", inputMode: "email" as const, maxLength: spec.maxLength };
    case "int":
      return { inputMode: "numeric" as const, maxLength: 10 };
    case "amount":
      return { inputMode: "decimal" as const, maxLength: 19 };
    default:
      //  item 12: the KYC field specs mirror the backend domain
      // verbatim (no dedicated phone KIND), so the telephone keyboard
      // rides on the spec KEY — a rendering hint only, zero schema
      // change.
      if (spec.key === "phone") {
        return { type: "tel", inputMode: "tel" as const, maxLength: spec.maxLength };
      }
      return { maxLength: spec.maxLength };
  }
}

export function KycProfileFields({
  memberType,
  idPrefix,
  values,
  errors,
  disabled,
  onChange,
}: Readonly<{
  memberType: MemberType;
  /** Distinguishes wizard vs edit instances in the same document. */
  idPrefix: string;
  values: Readonly<Record<string, string>>;
  errors: FieldErrors;
  disabled: boolean;
  onChange: (key: string, value: string) => void;
}>) {
  return (
    <div>
      {KYC_FORM_SECTIONS[memberType].map((section) => (
        <fieldset key={section.path.join(".")} className={styles.kycSection}>
          <legend className={styles.panelSubTitle}>{section.label}</legend>
          <div className={styles.fieldsGrid}>
            {section.fields.map((field) => {
              const key = kycFieldKey(section.path, field.key);
              const id = `${idPrefix}-${key.replace(/\./g, "-")}`;
              const label = field.required ? field.label : `${field.label} (optional)`;
              return (
                <FormField key={key} id={id} label={label} error={errors[key]}>
                  {(control) => (
                    <input
                      {...control}
                      {...inputProps(field)}
                      className={styles.input}
                      value={values[key] ?? ""}
                      disabled={disabled}
                      onChange={(event) => onChange(key, event.target.value)}
                    />
                  )}
                </FormField>
              );
            })}
          </div>
        </fieldset>
      ))}
    </div>
  );
}
