"use client";

/**
 * Member KYC drawer (the register's row drill-down): the profile and required-documents surfaces for
 * one member.
 *
 * - Reads under members:view (the server access-AUDITS every profile
 *   read); the edit affordance mounts only with
 *   members:edit; starting the wizard only with members:create. Pure
 *   UX — the server enforces every call (least disclosure).
 * - The profile is a RECORD read (staleTime 0): the version an edit
 *   pins is the freshest available; a stale submission is a 409 into
 *   the shared ConflictBanner (explicit reload-and-re-enter: the form
 *   re-initialises from the SERVER record and nothing is replayed).
 * - A 404 is the honest "no profile yet" state, never an error toast.
 * - Consent renders as state, not data (timestamp verbatim); granting
 *   is a one-way act named on the checkbox (never withdrawable).
 * - Every value renders through React text interpolation only; the
 *   informational monthly-income string renders verbatim (it is
 *   declared KYC data, not ledger money — kycSchemas.ts).
 */
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Button, Modal, Pill } from "@genesis/design-system";
import { STALE_TIME } from "@/lib/query";
import { isConflict } from "@/lib/errors";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { announce } from "@/modules/layout/announcer";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { FormField } from "@/modules/forms/FormField";
import { fromApiError, mergeFieldErrors, type FieldErrors } from "@/modules/forms/form-errors";
import { fetchBranch } from "@/modules/branches/api";
import { fetchMemberDetail } from "../api";
import { fetchKycProfile, updateKycProfile } from "../kycApi";
import { KYC_FORM_SECTIONS, type KycProfile } from "../kycSchemas";
import {
  buildProfilePayload,
  profileToFormValues,
  sectionData,
  validateProfilePayload,
} from "../kycForm";
import { TYPE_LABELS, type Member } from "../schemas";
import { KycProfileFields } from "./KycProfileFields";
import { KycDocumentsPanel } from "./KycDocumentsPanel";
import styles from "./Members.module.css";

function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

function ProfileView({ profile }: Readonly<{ profile: KycProfile }>) {
  return (
    <div>
      {KYC_FORM_SECTIONS[profile.member_type].map((section) => {
        const data = sectionData(profile.profile, section);
        return (
          <div key={section.path.join(".")} className={styles.summaryPanel}>
            <div className={styles.panelSubTitle}>{section.label}</div>
            <dl className={styles.summaryList}>
              {section.fields.map((field) => {
                const raw = data[field.key];
                return (
                  <div key={field.key} className={styles.summaryRow}>
                    <dt>{field.label}</dt>
                    <dd>{raw === null || raw === undefined ? "—" : String(raw)}</dd>
                  </div>
                );
              })}
            </dl>
          </div>
        );
      })}
    </div>
  );
}

export function MemberKycDrawer({
  member,
  onStartWizard,
  onClose,
}: Readonly<{
  member: Member;
  /** Opens the registration wizard for this member (members:create). */
  onStartWizard: () => void;
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const mayCreate = can(permissions.data, "members", "create");
  const mayEdit = can(permissions.data, "members", "edit");

  const profileQuery = useQuery({
    queryKey: ["members", "kyc-profile", member.id],
    queryFn: () => fetchKycProfile(member.id),
    // Record class: never serve a stale record into a version-pinned
    // edit (lib/query.ts).
    staleTime: STALE_TIME.record,
    retry: false,
  });

  // Advisory financial aggregates: the DETAIL read
  // carries four decimal strings rendered VERBATIM below (blocker
  // (a): no summing, no derived figures, ever). Record class: money
  // figures are never served stale.
  const detailQuery = useQuery({
    queryKey: ["members", "detail", member.id],
    queryFn: () => fetchMemberDetail(member.id),
    staleTime: STALE_TIME.record,
    retry: false,
  });

  // Branch attribution: the FRESH detail read's
  // nullable branch_id — NULL is the honest "unassigned" state (the
  // 0016 column is written only by the assignment route;
  // attribution is never invented). Resolving the branch NAME is a
  // settings:view read (GET /branches/{id}): without that grant this
  // drawer fetches NOTHING and renders the short id (the created_by
  // least-disclosure precedent).
  const maySettingsView = can(permissions.data, "settings", "view");
  const branchId = detailQuery.data?.branch_id ?? null;
  const branchQuery = useQuery({
    queryKey: ["branches", "detail", branchId ?? "none"],
    queryFn: () => {
      if (branchId === null) return Promise.reject(new Error("no branch on this member"));
      return fetchBranch(branchId);
    },
    enabled: branchId !== null && maySettingsView,
    staleTime: STALE_TIME.record,
    retry: false,
  });

  const [editing, setEditing] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [category, setCategory] = useState("");
  const [grantConsent, setGrantConsent] = useState(false);
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const update = useMutation({
    mutationFn: (input: {
      version: number;
      category: string;
      profile: Record<string, unknown>;
      dpa_consent?: true;
    }) =>
      updateKycProfile(
        member.id,
        input,
        // Versioned write (MATERIAL rule 3): the record version is part
        // of the body and therefore of the key material — a reload that
        // advanced it is a NEW intent, never a replay.
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({ op: "kyc-profile-update", memberId: member.id, input }),
        ),
      ),
    onSuccess: (updated) => {
      queryClient.setQueryData(["members", "kyc-profile", member.id], updated);
      setEditing(false);
      setGrantConsent(false);
      announce("KYC profile updated.");
    },
  });

  function startEditing(profile: KycProfile) {
    setValues(profileToFormValues(profile.member_type, profile.profile));
    setCategory(profile.category);
    setGrantConsent(false);
    setClientErrors({});
    update.reset();
    setEditing(true);
  }

  function reloadAfterConflict() {
    update.reset();
    setEditing(false);
    void queryClient.invalidateQueries({ queryKey: ["members", "kyc-profile", member.id] });
    announce("Profile reloaded after the conflict — nothing was changed. Re-enter the edit.");
  }

  function submitEdit(profile: KycProfile) {
    if (update.isPending) return;
    const payload = buildProfilePayload(profile.member_type, values);
    const found: Record<string, string> = { ...validateProfilePayload(profile.member_type, payload) };
    if (category.trim() === "") found["category"] = "Enter the member category.";
    setClientErrors(found);
    if (Object.keys(found).length > 0) return;
    update.mutate({
      version: profile.version,
      category: category.trim(),
      profile: payload,
      ...(grantConsent ? { dpa_consent: true as const } : {}),
    });
  }

  const errors = mergeFieldErrors(clientErrors, fromApiError(update.error));
  const profile = profileQuery.data;

  return (
    <Modal
      title={`Member KYC — ${member.name}`}
      onClose={onClose}
      closeDisabled={update.isPending}
      // W56-3: the drawer hosts version-pinned edit forms — dismissal
      // is the explicit ✕ or Escape only.
      dismissOnOverlay={false}
    >
      <div className={styles.docHead}>
        <span className={styles.cellSub}>{member.member_no}</span>
        <Pill>{TYPE_LABELS[member.type]}</Pill>
      </div>

      {/* Branch attribution: the fresh detail
          read's nullable branch_id, rendered honestly — NULL is the
          real "unassigned" state, never an invented default. The name
          resolves only under settings:view; otherwise the SHORT id
          renders with the full UUID on title (the created_by
          convention). */}
      {detailQuery.data !== undefined && (
        <div className={styles.summaryPanel}>
          <div className={styles.panelSubTitle}>Branch</div>
          <dl className={styles.summaryList}>
            <div className={styles.summaryRow}>
              <dt>Home branch</dt>
              <dd>
                {detailQuery.data.branch_id === null ? (
                  "— (unassigned; branches console assigns)"
                ) : branchQuery.data !== undefined ? (
                  branchQuery.data.name
                ) : (
                  <span title={detailQuery.data.branch_id}>
                    {detailQuery.data.branch_id.slice(0, 8)}
                  </span>
                )}
              </dd>
            </div>
          </dl>
        </div>
      )}

      {/* Dividend payout PREFERENCE: the fresh
          detail read's nullable dividend_payout, the server's own
          code-owned vocabulary token rendered VERBATIM — never
          re-labelled, never money math. NULL is the honest "not set"
          state (a preference is never invented). Stored preference
          ONLY: the distribution engine does not consume it
          (fence). */}
      {detailQuery.data !== undefined && (
        <div className={styles.summaryPanel}>
          <div className={styles.panelSubTitle}>Dividend payout</div>
          <dl className={styles.summaryList}>
            <div className={styles.summaryRow}>
              <dt>Preference</dt>
              <dd>
                {detailQuery.data.dividend_payout === null
                  ? "— (not set; the member edit records it)"
                  : detailQuery.data.dividend_payout}
              </dd>
            </div>
          </dl>
        </div>
      )}

      <div className={styles.summaryPanel}>
        <div className={styles.panelSubTitle}>Financial summary</div>
        {detailQuery.isPending && (
          <div className={styles.muted}>Loading financial summary…</div>
        )}
        {detailQuery.isError && <ErrorBanner error={detailQuery.error} />}
        {detailQuery.data !== undefined && (
          <dl className={styles.summaryList}>
            <div className={styles.summaryRow}>
              <dt>Deposits</dt>
              <dd>{detailQuery.data.aggregates.deposits_total}</dd>
            </div>
            <div className={styles.summaryRow}>
              <dt>Share capital</dt>
              <dd>{detailQuery.data.aggregates.shares_total}</dd>
            </div>
            <div className={styles.summaryRow}>
              <dt>Loans outstanding</dt>
              <dd>{detailQuery.data.aggregates.loans_outstanding}</dd>
            </div>
            <div className={styles.summaryRow}>
              <dt>Guarantees pledged</dt>
              <dd>{detailQuery.data.aggregates.guarantees_pledged}</dd>
            </div>
          </dl>
        )}
      </div>

      {profileQuery.isPending && <div className={styles.muted}>Loading KYC profile…</div>}

      {profileQuery.isError && isNotFound(profileQuery.error) && (
        <div className={styles.summaryPanel}>
          <p className={styles.panelNote}>
            No KYC profile on record for this member — the registration wizard captures
            the per-type profile, the DPA-2019 consent and the document checklist.
          </p>
          {mayCreate ? (
            <Button type="button" variant="primary" onClick={onStartWizard}>
              Start KYC wizard
            </Button>
          ) : (
            <p className={styles.panelNote}>
              Your role has no members create permission — registration affordances are
              not offered.
            </p>
          )}
        </div>
      )}
      {profileQuery.isError && !isNotFound(profileQuery.error) && (
        <ErrorBanner error={profileQuery.error} />
      )}

      {profile !== undefined && !editing && (
        <div>
          <dl className={styles.summaryList}>
            <div className={styles.summaryRow}>
              <dt>Category</dt>
              <dd>{profile.category}</dd>
            </div>
            <div className={styles.summaryRow}>
              <dt>DPA-2019 consent</dt>
              <dd>
                {profile.dpa_consent_at === null
                  ? "Not captured"
                  : `Captured ${profile.dpa_consent_at}`}
              </dd>
            </div>
          </dl>
          <ProfileView profile={profile} />
          {mayEdit && (
            <div className={styles.actions}>
              <Button type="button" onClick={() => startEditing(profile)}>
                Edit profile
              </Button>
            </div>
          )}
        </div>
      )}

      {profile !== undefined && editing && (
        <form
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            submitEdit(profile);
          }}
        >
          <FormField id="kyc-edit-category" label="Member category" error={errors["category"]}>
            {(control) => (
              <input
                {...control}
                className={styles.input}
                maxLength={60}
                value={category}
                disabled={update.isPending}
                onChange={(event) => setCategory(event.target.value)}
              />
            )}
          </FormField>
          <KycProfileFields
            memberType={profile.member_type}
            idPrefix="kyc-edit"
            values={values}
            errors={errors}
            disabled={update.isPending}
            onChange={(key, value) => setValues((prev) => ({ ...prev, [key]: value }))}
          />
          {profile.dpa_consent_at === null && (
            <div className={styles.consentRow}>
              <input
                id="kyc-edit-consent"
                type="checkbox"
                checked={grantConsent}
                disabled={update.isPending}
                onChange={(event) => setGrantConsent(event.target.checked)}
              />
              <label htmlFor="kyc-edit-consent">
                Record DPA-2019 consent with this edit (one-way: consent can never be
                withdrawn once captured)
              </label>
            </div>
          )}
          {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
          <ConflictBanner error={update.error} onReload={reloadAfterConflict} />
          {update.isError && !isConflict(update.error) && <ErrorBanner error={update.error} />}
          <div className={styles.actions}>
            <Button
              type="button"
              onClick={() => {
                setEditing(false);
                update.reset();
              }}
              disabled={update.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={update.isPending}>
              {update.isPending ? "Saving…" : "Save profile"}
            </Button>
          </div>
        </form>
      )}

      <KycDocumentsPanel memberId={member.id} memberType={member.type} />
    </Modal>
  );
}
