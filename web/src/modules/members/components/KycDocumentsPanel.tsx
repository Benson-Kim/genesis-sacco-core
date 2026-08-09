"use client";

/**
 * KYC required-documents checklist panel (documents routes; prototype `docsFor` checklist).
 * Embedded by BOTH the registration wizard's final step and the member
 * KYC drawer (one copy, reuse-first).
 *
 * HONEST SCOPING (stated on-screen): binary document UPLOAD has NO
 * backend contract — storage is deferred behind ADR-0003, so the
 * prototype's per-document "Upload" button cannot be honestly shipped.
 * This panel manages the checklist METADATA the contract does provide:
 * tracking a required document and moving it through the review status
 * machine (pending, received, verified, rejected — server-validated).
 *
 * - Reads under members:view (server access-audits every checklist read — blocker f); tracking under members:create; review moves
 *   and expiry corrections under members:edit. Affordances mount only
 *   with the grant — pure UX; the server enforces every call (1.6).
 * - Keyset list ONLY (scalability). The checklist is 5 code-owned types
 *   per member type, so the "not yet tracked" affordances derive from
 *   the loaded pages verbatim.
 * - Review moves and expiry edits PIN the row version (409 on stale —
 *   ConflictBanner, reload-never-replays); Idempotency-Key per the
 *   MATERIAL rule (versioned writes fold the version acted on).
 * - Every rendered string (doc types, dates) renders through React
 *   text interpolation only.
 */
import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Button, Pill } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { announce } from "@/modules/layout/announcer";
import { isConflict } from "@/lib/errors";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { FormField } from "@/modules/forms/FormField";
import { createKycDocument, fetchKycDocumentsPage, updateKycDocument } from "../kycApi";
import {
  DOCUMENT_ACTION_LABELS,
  DOCUMENT_STATUS_LABELS,
  DOCUMENT_TRANSITIONS,
  KYC_DATE_RE,
  KYC_DOC_TYPES,
  docTypeLabel,
  type DocumentStatus,
  type KycDocument,
} from "../kycSchemas";
import type { MemberType } from "../schemas";
import styles from "./Members.module.css";

function statusPill(status: DocumentStatus) {
  switch (status) {
    case "pending":
      return <Pill bg="goldSoft" color="navy">{DOCUMENT_STATUS_LABELS.pending}</Pill>;
    case "received":
      return <Pill bg="navySoft" color="navy">{DOCUMENT_STATUS_LABELS.received}</Pill>;
    case "verified":
      return <Pill bg="emeraldSoft" color="emerald">{DOCUMENT_STATUS_LABELS.verified}</Pill>;
    case "rejected":
      return <Pill bg="brickSoft" color="brick">{DOCUMENT_STATUS_LABELS.rejected}</Pill>;
  }
}

export function KycDocumentsPanel({
  memberId,
  memberType,
}: Readonly<{
  memberId: string;
  memberType: MemberType;
}>) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const mayCreate = can(permissions.data, "members", "create");
  const mayEdit = can(permissions.data, "members", "edit");

  const list = useKeysetList<KycDocument>({
    queryKey: ["members", "kyc-documents", memberId],
    fetchPage: (cursor) => fetchKycDocumentsPage(memberId, cursor),
  });

  const [expiryEdit, setExpiryEdit] = useState<{ documentId: string; value: string } | null>(null);
  const [expiryError, setExpiryError] = useState("");
  const trackSlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  const moveSlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  const expirySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ["members", "kyc-documents", memberId] });
  }

  const track = useMutation({
    mutationFn: (docType: string) =>
      createKycDocument(
        memberId,
        { doc_type: docType },
        idempotencyKeyFor(
          trackSlot.current,
          JSON.stringify({ op: "kyc-document-create", memberId, docType }),
        ),
      ),
    onSuccess: (created) => {
      invalidate();
      announce(`${docTypeLabel(created.doc_type)} added to the checklist.`);
    },
  });

  const move = useMutation({
    mutationFn: (input: { document: KycDocument; target: DocumentStatus }) =>
      updateKycDocument(
        memberId,
        input.document.id,
        { version: input.document.version, status: input.target },
        // Versioned write (MATERIAL rule 3): the version acted on is
        // folded, so a reload that advanced it is a NEW intent.
        idempotencyKeyFor(
          moveSlot.current,
          JSON.stringify({
            op: "kyc-document-status",
            memberId,
            documentId: input.document.id,
            version: input.document.version,
            target: input.target,
          }),
        ),
      ),
    onSuccess: (updated) => {
      invalidate();
      announce(
        `${docTypeLabel(updated.doc_type)} is now ${DOCUMENT_STATUS_LABELS[updated.status]}.`,
      );
    },
  });

  const saveExpiry = useMutation({
    mutationFn: (input: { document: KycDocument; expiresAt: string | null }) =>
      updateKycDocument(
        memberId,
        input.document.id,
        { version: input.document.version, expires_at: input.expiresAt },
        idempotencyKeyFor(
          expirySlot.current,
          JSON.stringify({
            op: "kyc-document-expiry",
            memberId,
            documentId: input.document.id,
            version: input.document.version,
            expiresAt: input.expiresAt,
          }),
        ),
      ),
    onSuccess: (updated) => {
      invalidate();
      setExpiryEdit(null);
      announce(`${docTypeLabel(updated.doc_type)} expiry updated.`);
    },
  });

  const rows = list.data?.pages.flatMap((page) => page.items) ?? [];
  const trackedTypes = new Set(rows.map((row) => row.doc_type));
  const missing = KYC_DOC_TYPES[memberType].filter((docType) => !trackedTypes.has(docType));

  function submitExpiry(doc: KycDocument) {
    if (saveExpiry.isPending || expiryEdit === null) return;
    const raw = expiryEdit.value.trim();
    if (raw !== "" && !KYC_DATE_RE.test(raw)) {
      setExpiryError("Use the YYYY-MM-DD date form.");
      return;
    }
    setExpiryError("");
    // Blank input = EXPLICIT clear (the contract's null branch);
    // a kept expiry is simply not edited.
    saveExpiry.mutate({ document: doc, expiresAt: raw === "" ? null : raw });
  }

  return (
    <section aria-label="Required documents">
      <h3 className={styles.panelTitle}>Required documents</h3>
      <p className={styles.panelNote}>
        Checklist metadata only: file upload has no backend contract yet
        (storage decision ADR-0003) — track each document and record its
        review status here.
      </p>
      {list.isPending && <div className={styles.muted}>Loading checklist…</div>}
      {list.isError && <ErrorBanner error={list.error} />}
      {track.isError && <ErrorBanner error={track.error} />}
      {/* One copy of the 409 reload-and-re-enter flow (reuse-first);
          ErrorBanner covers the non-409s — never double-reported. */}
      <ConflictBanner
        error={move.error}
        onReload={() => {
          invalidate();
          announce("Checklist reloaded after the conflict — nothing was changed.");
        }}
      />
      {move.isError && !isConflict(move.error) && <ErrorBanner error={move.error} />}
      <ConflictBanner
        error={saveExpiry.error}
        onReload={() => {
          invalidate();
          setExpiryEdit(null);
          announce("Checklist reloaded after the conflict — nothing was changed.");
        }}
      />
      {saveExpiry.isError && !isConflict(saveExpiry.error) && (
        <ErrorBanner error={saveExpiry.error} />
      )}
      <ul className={styles.docList}>
        {rows.map((doc) => (
          <li key={doc.id} className={styles.docRow}>
            <div className={styles.docHead}>
              <span className={styles.cellStrong}>{docTypeLabel(doc.doc_type)}</span>
              {statusPill(doc.status)}
              <span className={styles.cellSub}>
                {doc.expires_at === null ? "No expiry" : `Expires ${doc.expires_at}`}
              </span>
            </div>
            {mayEdit && (
              <div className={styles.docActions}>
                {DOCUMENT_TRANSITIONS[doc.status].map((target) => (
                  <Button
                    key={target}
                    type="button"
                    disabled={move.isPending}
                    onClick={() => {
                      if (move.isPending) return;
                      move.mutate({ document: doc, target });
                    }}
                  >
                    {DOCUMENT_ACTION_LABELS[target]}
                  </Button>
                ))}
                {expiryEdit?.documentId === doc.id ? (
                  <span className={styles.expiryEditor}>
                    <FormField
                      id={`kyc-expiry-${doc.id}`}
                      label="Expiry (blank clears it)"
                      error={expiryError === "" ? undefined : expiryError}
                    >
                      {(control) => (
                        <input
                          {...control}
                          className={styles.input}
                          type="date"
                          value={expiryEdit.value}
                          onChange={(event) =>
                            setExpiryEdit({ documentId: doc.id, value: event.target.value })
                          }
                        />
                      )}
                    </FormField>
                    <Button
                      type="button"
                      disabled={saveExpiry.isPending}
                      onClick={() => submitExpiry(doc)}
                    >
                      {saveExpiry.isPending ? "Saving…" : "Save expiry"}
                    </Button>
                    <Button
                      type="button"
                      disabled={saveExpiry.isPending}
                      onClick={() => {
                        setExpiryEdit(null);
                        setExpiryError("");
                      }}
                    >
                      Cancel
                    </Button>
                  </span>
                ) : (
                  <Button
                    type="button"
                    onClick={() =>
                      setExpiryEdit({ documentId: doc.id, value: doc.expires_at ?? "" })
                    }
                  >
                    Edit expiry
                  </Button>
                )}
              </div>
            )}
          </li>
        ))}
        {!list.isPending && rows.length === 0 && (
          <li className={styles.muted}>No documents tracked yet.</li>
        )}
      </ul>
      {list.hasNextPage && (
        <Button
          type="button"
          onClick={() => list.fetchNextPage()}
          disabled={list.isFetchingNextPage}
        >
          {list.isFetchingNextPage ? "Loading…" : "Load more"}
        </Button>
      )}
      {mayCreate && missing.length > 0 && (
        <div className={styles.docMissing}>
          <div className={styles.panelSubTitle}>Not yet tracked</div>
          <div className={styles.docActions}>
            {missing.map((docType) => (
              <Button
                key={docType}
                type="button"
                disabled={track.isPending}
                onClick={() => {
                  if (track.isPending) return;
                  track.mutate(docType);
                }}
              >
                {`Track ${docTypeLabel(docType)}`}
              </Button>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
