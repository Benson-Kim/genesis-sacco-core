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
import {
  Button,
  Card,
  ErrorMessage,
  type FieldErrors,
  fromApiError,
  grid,
  Input,
  mergeFieldErrors,
  Select,
} from "@genesis/design-system";
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

  const isLast = (index: number) => index === bands.length - 1;

  return (
    <form onSubmit={submit} noValidate>
      {/* Two-card layout matching the prototype Roles & permissions pattern */}
      <div className={grid.sideMain}>

        {/* ── Left card: committee size + quorum (flush rows, no padding) ── */}
        <Card padded={false} style={{ alignSelf: "flex-start", overflow: "hidden" }}>
          <ul className={styles.approvalSideList} aria-label="Committee settings">
            <li className={styles.approvalSideRow}>
              <label className={styles.approvalSideLabel} htmlFor="approval-committee-size">
                Committee size
              </label>
              <Input
                id="approval-committee-size"
                inputMode="numeric"
                maxLength={2}
                value={committeeSize}
                disabled={!mayEdit}
                aria-describedby={fieldErrors["committee_size"] !== undefined ? "committee-size-error" : undefined}
                aria-invalid={fieldErrors["committee_size"] !== undefined ? true : undefined}
                onChange={(event) => setCommitteeSize(event.target.value)}
              />
              {fieldErrors["committee_size"] !== undefined && (
                <ErrorMessage id="committee-size-error">{fieldErrors["committee_size"]}</ErrorMessage>
              )}
            </li>
            <li className={styles.approvalSideRow}>
              <label className={styles.approvalSideLabel} htmlFor="approval-committee-quorum">
                Quorum (approvals)
              </label>
              <Input
                id="approval-committee-quorum"
                inputMode="numeric"
                maxLength={2}
                value={committeeQuorum}
                disabled={!mayEdit}
                aria-describedby={fieldErrors["committee_quorum"] !== undefined ? "committee-quorum-error" : undefined}
                aria-invalid={fieldErrors["committee_quorum"] !== undefined ? true : undefined}
                onChange={(event) => setCommitteeQuorum(event.target.value)}
              />
              {fieldErrors["committee_quorum"] !== undefined && (
                <ErrorMessage id="committee-quorum-error">{fieldErrors["committee_quorum"]}</ErrorMessage>
              )}
            </li>
          </ul>
          {/* Save button flush at the bottom of the left card */}
          <div className={styles.approvalSideSave}>
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
          </div>
        </Card>

        {/* ── Right card: approval authority table ── */}
        <Card>
          <h2 className={styles.approvalTitle}>Approval authority &amp; committee</h2>

          {fieldErrors["approval_bands"] !== undefined && (
            <ErrorMessage>{fieldErrors["approval_bands"]}</ErrorMessage>
          )}

          <table className={styles.approvalTable}>
            <thead className={styles.approvalThead}>
              <tr>
                <th className={styles.approvalTh} scope="col">Authority</th>
                <th className={`${styles.approvalTh} ${styles.approvalThCeiling}`} scope="col">
                  Approves up to (KES)
                </th>
                {mayEdit && editing && <th className={styles.approvalTh} scope="col" />}
              </tr>
            </thead>
            <tbody className={styles.approvalTbody}>
              {bands.length === 0 && (
                <tr>
                  <td
                    colSpan={mayEdit && editing ? 3 : 2}
                    className={styles.approvalTdAuthority}
                    style={{ color: "var(--sub)", fontWeight: "normal" }}
                  >
                    No approval bands configured.
                  </td>
                </tr>
              )}
              {bands.map((row, index) => (
                <tr key={index}>
                  <td className={styles.approvalTdAuthority}>
                    {editing ? (
                      <>
                        <Select
                          id={`approval-band-${index}-authority`}
                          aria-label={`Band ${index + 1} authority`}
                          aria-describedby={
                            fieldErrors[`approval_bands.${index}.authority`] !== undefined
                              ? `approval-band-${index}-authority-error`
                              : undefined
                          }
                          aria-invalid={
                            fieldErrors[`approval_bands.${index}.authority`] !== undefined
                              ? true
                              : undefined
                          }
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
                        </Select>
                        {fieldErrors[`approval_bands.${index}.authority`] !== undefined && (
                          <ErrorMessage id={`approval-band-${index}-authority-error`}>
                            {fieldErrors[`approval_bands.${index}.authority`]}
                          </ErrorMessage>
                        )}
                      </>
                    ) : (
                      row.authority || <span style={{ color: "var(--sub)" }}>—</span>
                    )}
                  </td>

                  <td className={styles.approvalTdCeiling}>
                    <Input
                      id={`approval-band-${index}-ceiling`}
                      aria-label={`Band ${index + 1} approves up to (KES)`}
                      aria-describedby={
                        fieldErrors[`approval_bands.${index}.ceiling`] !== undefined
                          ? `approval-band-${index}-ceiling-error`
                          : undefined
                      }
                      aria-invalid={
                        fieldErrors[`approval_bands.${index}.ceiling`] !== undefined
                          ? true
                          : undefined
                      }
                      className={styles.ceilingInput}
                      inputMode="decimal"
                      maxLength={19}
                      placeholder={isLast(index) ? "No limit" : undefined}
                      value={row.ceiling}
                      disabled={!mayEdit}
                      onChange={(event) => setBand(index, { ceiling: event.target.value })}
                    />
                    {fieldErrors[`approval_bands.${index}.ceiling`] !== undefined && (
                      <ErrorMessage id={`approval-band-${index}-ceiling-error`}>
                        {fieldErrors[`approval_bands.${index}.ceiling`]}
                      </ErrorMessage>
                    )}
                  </td>

                  {mayEdit && editing && (
                    <td className={styles.approvalTdActions}>
                      <button
                        type="button"
                        className={styles.removeBandBtn}
                        aria-label={`Remove band ${index + 1}`}
                        onClick={() => setBands((prev) => prev.filter((_, i) => i !== index))}
                      >
                        ×
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>

          {mayEdit && editing && (
            <div className={styles.addBandRow}>
              <Button
                type="button"
                onClick={() => setBands((prev) => [...prev, { authority: "", ceiling: "" }])}
              >
                + Add band
              </Button>
            </div>
          )}
        </Card>

      </div>
    </form>
  );
}
