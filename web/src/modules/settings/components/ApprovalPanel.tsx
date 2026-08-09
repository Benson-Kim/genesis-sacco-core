"use client";

/**
 * Approval matrix tab: committee size/quorum and the
 * per-authority approval bands (cumulative-ceiling semantics — the
 * band at index i covers amounts above the previous band's ceiling).
 *
 * Authorities are the CODE-OWNED platform role names (UX list mirrors
 * backend ROLE_NAMES; the server validates the vocabulary and band
 * ordering — 422 field messages render inline). Ceilings are decimal
 * STRINGS submitted verbatim; the client never compares amounts.
 * WYSIWYG per-tab write with the loaded `version`; blank clears a key.
 */
import { useState, type FormEvent } from "react";
import { Button, Card } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import { fromApiError, mergeFieldErrors, type FieldErrors } from "@/modules/forms/form-errors";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import type { UpdateSettingsInput } from "../api";
import {
  AMOUNT_RE,
  AUTHORITY_ROLE_NAMES,
  parseOptionalInt,
  type Settings,
} from "../schemas";
import { SettingsSaveControls, useSettingsSaveFlow } from "./SettingsSaveFlow";
import styles from "./Settings.module.css";

interface BandRow {
  authority: string;
  /** Ceiling (KES decimal string); "" = no limit (last band only). */
  ceiling: string;
}

const AMOUNT_MSG = "Amount like 500000 or 500000.50 (max 2dp)";

export function ApprovalPanel({
  settings,
  editing,
}: Readonly<{ settings: Settings; editing: boolean }>) {
  const permissions = usePermissions();
  const mayEdit = can(permissions.data, "settings", "edit");
  const flow = useSettingsSaveFlow("approval");

  const [committeeSize, setCommitteeSize] = useState(
    settings.committee_size === null ? "" : String(settings.committee_size),
  );
  const [committeeQuorum, setCommitteeQuorum] = useState(
    settings.committee_quorum === null ? "" : String(settings.committee_quorum),
  );
  const [bands, setBands] = useState<BandRow[]>(
    (settings.approval_bands ?? []).map((band) => ({
      // An authority outside the code-owned list degrades to unpicked
      // at load so it is never silently resubmitted (F-A5 precedent).
      authority: (AUTHORITY_ROLE_NAMES as readonly string[]).includes(band.authority)
        ? band.authority
        : "",
      ceiling: band.max_amount ?? "",
    })),
  );
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});

  const fieldErrors = mergeFieldErrors(clientErrors, fromApiError(flow.save.error));

  function setBand(index: number, patch: Partial<BandRow>) {
    setBands((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (flow.save.isPending) return;

    const errors: Record<string, string> = {};
    const pCommitteeSize = parseOptionalInt(committeeSize, 1, 25);
    if (!pCommitteeSize.ok) errors["committee_size"] = pCommitteeSize.message;
    const pCommitteeQuorum = parseOptionalInt(committeeQuorum, 1, 25);
    if (!pCommitteeQuorum.ok) errors["committee_quorum"] = pCommitteeQuorum.message;

    bands.forEach((row, index) => {
      const isLast = index === bands.length - 1;
      if (row.authority === "") {
        errors[`approval_bands.${index}.authority`] = "Pick an authority";
      }
      const ceiling = row.ceiling.trim();
      if (ceiling === "") {
        if (!isLast) {
          errors[`approval_bands.${index}.ceiling`] = "Only the last band may be unbounded";
        }
      } else if (!AMOUNT_RE.test(ceiling)) {
        errors[`approval_bands.${index}.ceiling`] = AMOUNT_MSG;
      }
    });

    if (Object.keys(errors).length > 0) {
      setClientErrors(errors);
      return;
    }
    setClientErrors({});

    const input: UpdateSettingsInput = {
      version: settings.version,
      committee_size: pCommitteeSize.ok ? pCommitteeSize.value : null,
      committee_quorum: pCommitteeQuorum.ok ? pCommitteeQuorum.value : null,
      approval_bands:
        bands.length === 0
          ? null
          : bands.map((row) => ({
              authority: row.authority,
              max_amount: row.ceiling.trim() === "" ? null : row.ceiling.trim(),
            })),
    };
    flow.requestSave(input);
  }

  return (
    <form onSubmit={submit} noValidate>
      <Card>
        <h2 className={styles.sectionTitle}>Approval authority &amp; committee</h2>
        {fieldErrors["approval_bands"] !== undefined && (
          <div className={styles.fieldError} role="alert">
            {fieldErrors["approval_bands"]}
          </div>
        )}
        {bands.length === 0 && (
          <div className={styles.formNote}>
            No approval bands configured — authority-band enforcement runs on
            documented fallbacks.
          </div>
        )}
        {bands.map((row, index) => (
          <div key={index}>
            <div className={styles.subhead}>
              Band {index + 1}
              {index > 0 ? " — covers amounts above the previous ceiling" : ""}
            </div>
            <div className={styles.fieldsGrid}>
              <FormField
                id={`approval-band-${index}-authority`}
                label={`Band ${index + 1} authority`}
                error={fieldErrors[`approval_bands.${index}.authority`]}
              >
                {(control) => (
                  <select
                    {...control}
                    className={styles.select}
                    value={
                      (AUTHORITY_ROLE_NAMES as readonly string[]).includes(row.authority)
                        ? row.authority
                        : ""
                    }
                    disabled={!mayEdit}
                    onChange={(event) => setBand(index, { authority: event.target.value })}
                  >
                    <option value="">Pick an authority</option>
                    {AUTHORITY_ROLE_NAMES.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                )}
              </FormField>
              <FormField
                id={`approval-band-${index}-ceiling`}
                label={`Band ${index + 1} approves up to (KES)`}
                error={fieldErrors[`approval_bands.${index}.ceiling`]}
                hint={index === bands.length - 1 ? "Blank = no limit (last band only)." : undefined}
              >
                {(control) => (
                  <input
                    {...control}
                    className={styles.input}
                    inputMode="decimal"
                    maxLength={19}
                    value={row.ceiling}
                    disabled={!mayEdit}
                    onChange={(event) => setBand(index, { ceiling: event.target.value })}
                  />
                )}
              </FormField>
            </div>
            {mayEdit && (
              <div className={styles.bandActions}>
                <Button
                  type="button"
                  onClick={() => setBands((prev) => prev.filter((_, i) => i !== index))}
                >
                  Remove band {index + 1}
                </Button>
              </div>
            )}
          </div>
        ))}
        {mayEdit && (
          <div className={styles.bandActions}>
            <Button
              type="button"
              onClick={() => setBands((prev) => [...prev, { authority: "", ceiling: "" }])}
            >
              + Add band
            </Button>
          </div>
        )}

        <div className={styles.subhead}>Committee</div>
        <div className={styles.fieldsGrid}>
          <FormField
            id="approval-committee-size"
            label="Committee size"
            error={fieldErrors["committee_size"]}
            hint="Blank = not configured."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="numeric"
                maxLength={2}
                value={committeeSize}
                disabled={!mayEdit}
                onChange={(event) => setCommitteeSize(event.target.value)}
              />
            )}
          </FormField>
          <FormField
            id="approval-committee-quorum"
            label="Quorum (approvals)"
            error={fieldErrors["committee_quorum"]}
            hint="Read at vote time — never rewrites past votes."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="numeric"
                maxLength={2}
                value={committeeQuorum}
                disabled={!mayEdit}
                onChange={(event) => setCommitteeQuorum(event.target.value)}
              />
            )}
          </FormField>
        </div>
        <SettingsSaveControls
          flow={flow}
          mayEdit={mayEdit && editing}
          buttonLabel="Save matrix"
          confirmTitle="Apply approval matrix"
          confirmPhrase="approval"
          settings={settings}
        >
          <div className={styles.formNote}>
            Authority ceilings and quorum gate money-moving approvals from
            the moment they are saved (quorum is read at vote time). Blank
            fields CLEAR their stored value.
          </div>
        </SettingsSaveControls>
      </Card>
    </form>
  );
}
