"use client";

/**
 * Member-KYC registration wizard (the prototype's per-type registration wizard, `vAdd()` in genesis_prestige_app.html, restored over the REAL contract).
 *
 * The prototype's step 1 (member type + identity) IS the existing
 * quick-registration drawer (POST /members — untouched); this wizard
 * carries the remaining steps for the created member:
 *   1. KYC details — the per-type sections (forms map, field-for-field
 *      against domain/member_kyc.py);
 *   2. Membership & consent — the member category and the DPA-2019
 *      consent capture (the two membership-step inputs the contract provides; the rest of the prototype's membership step has NO backend contract and is recorded on, stated below);
 *   3. Documents & review — the required-documents checklist
 *      (metadata; upload deferred behind ADR-0003) and the summary.
 *
 * - Mounted only with members:create (the server enforces regardless, least disclosure). ONE profile POST per wizard: Idempotency-Key follows
 *   the MATERIAL rule (simple create — op + full canonical body);
 *   double-submit is disabled AND short-circuited.
 * - A create-409 (profile already exists, or key reuse with a changed
 *   hash) has no record to reload — the banner explains the remedy
 *   (open the member's KYC drawer; the profile is already there).
 * - Client Zod pre-validation names fields inline; the server verdict
 *   (422 with field locations only — values are never echoed) wins.
 * - W56-3: dismissal is the explicit ✕ or Escape — a stray overlay
 *   click never discards a half-completed registration.
 */
import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Button, Modal } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { FormField } from "@/modules/forms/FormField";
import { fromApiError, mergeFieldErrors, type FieldErrors } from "@/modules/forms/form-errors";
import { createKycProfile } from "../kycApi";
import { kycProfileCreateSchema, type KycProfile } from "../kycSchemas";
import { buildProfilePayload, validateProfilePayload } from "../kycForm";
import { TYPE_LABELS, type Member } from "../schemas";
import { KycProfileFields } from "./KycProfileFields";
import { KycDocumentsPanel } from "./KycDocumentsPanel";
import { StepRail } from "./StepRail";
import styles from "./Members.module.css";

export function KycWizard({
  member,
  onClose,
}: Readonly<{
  member: Member;
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState(0);
  const [values, setValues] = useState<Record<string, string>>({});
  const [category, setCategory] = useState("");
  const [consent, setConsent] = useState(false);
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [profile, setProfile] = useState<KycProfile | null>(null);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const create = useMutation({
    mutationFn: (input: {
      category: string;
      profile: Record<string, unknown>;
      dpa_consent: boolean;
    }) =>
      createKycProfile(
        member.id,
        input,
        // Simple create (MATERIAL rule 2): op + the full canonical
        // body — identical retries reuse the key, edits rotate it.
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({ op: "kyc-profile-create", memberId: member.id, input }),
        ),
      ),
    onSuccess: (created) => {
      setProfile(created);
      setStep(2);
      void queryClient.invalidateQueries({ queryKey: ["members", "kyc-profile", member.id] });
      announce("KYC profile created.");
    },
  });

  const errors = mergeFieldErrors(clientErrors, fromApiError(create.error));

  function validateDetails(): boolean {
    const payload = buildProfilePayload(member.type, values);
    const found = validateProfilePayload(member.type, payload);
    setClientErrors(found);
    return Object.keys(found).length === 0;
  }

  function submit() {
    if (create.isPending || profile !== null) return;
    const payload = buildProfilePayload(member.type, values);
    const profileErrors = validateProfilePayload(member.type, payload);
    const metaVerdict = kycProfileCreateSchema.safeParse({
      category: category.trim(),
      dpa_consent: consent,
    });
    const metaErrors: Record<string, string> = {};
    if (!metaVerdict.success) metaErrors["category"] = "Enter the member category.";
    const found = { ...profileErrors, ...metaErrors };
    setClientErrors(found);
    if (Object.keys(found).length > 0) return;
    create.mutate({ category: category.trim(), profile: payload, dpa_consent: consent });
  }

  return (
    <Modal
      title={`New member registration — ${member.name}`}
      onClose={onClose}
      closeDisabled={create.isPending}
      // W56-3: never discard a half-completed registration on a stray
      // overlay click.
      dismissOnOverlay={false}
    >
      {/* The identity step is behind us: this surface renders the SAME
          four-step rail as the core drawer, offset by one. */}
      <StepRail current={step + 1} />
      <p className={styles.panelNote}>
        {TYPE_LABELS[member.type]} member {member.member_no}. The identity step is already
        complete — this wizard captures the KYC profile and the document checklist.
      </p>

      {step === 0 && (
        <div>
          <KycProfileFields
            memberType={member.type}
            idPrefix="kyc-wizard"
            values={values}
            errors={errors}
            disabled={create.isPending}
            onChange={(key, value) => setValues((prev) => ({ ...prev, [key]: value }))}
          />
          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="primary"
              onClick={() => {
                if (validateDetails()) setStep(1);
              }}
            >
              Continue
            </Button>
          </div>
        </div>
      )}

      {step === 1 && (
        <div>
          <FormField
            id="kyc-wizard-category"
            label="Member category"
            error={errors["category"]}
            hint="For example Ordinary — the tenant's own vocabulary."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                maxLength={60}
                value={category}
                disabled={create.isPending}
                onChange={(event) => setCategory(event.target.value)}
              />
            )}
          </FormField>
          <div className={styles.consentRow}>
            <input
              id="kyc-wizard-consent"
              type="checkbox"
              checked={consent}
              disabled={create.isPending}
              onChange={(event) => setConsent(event.target.checked)}
            />
            <label htmlFor="kyc-wizard-consent">
              Data Protection Act 2019 consent captured (recorded server-side; it can be
              granted later, but never withdrawn)
            </label>
          </div>
          {/* Dividend payout PREFERENCE (the authorized contract): the member record's stored
              preference, the server's code-owned vocabulary token
              rendered VERBATIM; NULL is the honest "not set" state,
              never an invented default. */}
          <dl className={styles.summaryList}>
            <div className={styles.summaryRow}>
              <dt>Dividend payout preference</dt>
              <dd>
                {member.dividend_payout === null
                  ? "Not set (recorded on the member record when chosen)"
                  : member.dividend_payout}
              </dd>
            </div>
          </dl>
          <p className={styles.panelNote}>
            Registration fee, share capital, contribution amounts and method, and
            recruited-by are not yet on the member record — nothing is faked here.
            Home branch is assigned through the branches console; the dividend
            payout preference above is the member record&apos;s stored value.
          </p>
          {create.isError && (
            <>
              <ErrorBanner error={create.error} />
              <p className={styles.panelNote} role="note">
                If this member&apos;s profile already exists (a 409), open their KYC drawer
                from the register instead — nothing was created twice.
              </p>
            </>
          )}
          <div className={styles.actions}>
            <Button type="button" onClick={() => setStep(0)} disabled={create.isPending}>
              ‹ Back
            </Button>
            <Button
              type="button"
              variant="primary"
              disabled={create.isPending}
              onClick={submit}
            >
              {create.isPending ? "Creating…" : "Create KYC profile"}
            </Button>
          </div>
        </div>
      )}

      {step === 2 && profile !== null && (
        <div>
          <div className={styles.summaryPanel}>
            <div className={styles.panelSubTitle}>Summary</div>
            <dl className={styles.summaryList}>
              <div className={styles.summaryRow}>
                <dt>Type</dt>
                <dd>{TYPE_LABELS[member.type]}</dd>
              </div>
              <div className={styles.summaryRow}>
                <dt>Category</dt>
                <dd>{profile.category}</dd>
              </div>
              <div className={styles.summaryRow}>
                <dt>DPA-2019 consent</dt>
                <dd>{profile.dpa_consent_at === null ? "Not captured" : "Captured ✓"}</dd>
              </div>
            </dl>
          </div>
          <KycDocumentsPanel memberId={member.id} memberType={member.type} />
          <div className={styles.actions}>
            <Button type="button" variant="primary" onClick={onClose}>
              Done
            </Button>
          </div>
        </div>
      )}
    </Modal>
  );
}
