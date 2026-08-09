"use client";

/**
 * Corrections console (follow-on): the operator workbench for the fraud-channel
 * surfaces — repayment adjustments (maker-checker), misc
 * fees, committee loan write-offs and the issue- recovery receipts.
 *
 * REGISTERS (the contract follow-up DELIVERED as a human-authorized read expansion):
 * the contract now exposes keyset LIST reads, so the checker
 * works the pending-adjustments register (pending-first — the
 * server's order, never re-sorted locally) and the committee works
 * the write-off register (live-first) — no hand-carried id needed.
 * The by-id lookup fields remain as a fallback (e.g. an id from the
 * audit trail); by-id reads and all mutations are UNTOUCHED.",
 *
 * Security posture (transactions/exits precedent):
 * - Route guard is corrections:view (RequireModule); the create
 *   affordances mount only with corrections:create, checker/committee
 *   affordances inside the drawers only with corrections:approve —
 *   pure UX; the server enforces every call (least disclosure, deny by default: corrections carry their OWN permission strings, never generic transactions:edit).
 * - Every rendered string is attacker-influenced data; it renders
 *   exclusively through React text interpolation (gate-tested).
 * - MONEY (blocker (a)): no figure exists on this screen at all —
 *   every money string renders inside the drawers, verbatim from the
 *   API.
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import { Button, Card } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { FormField } from "@/modules/forms/FormField";
import { fmtDateTime, fmtKes, isUuid } from "@/lib/format";
import grid from "@/modules/layout/grid.module.css";
import { fetchAdjustmentsPage, fetchWriteOffsPage } from "../api";
import type { AdjustmentRecord, WriteOffRecord } from "../schemas";
import { adjustmentStatusPill, writeOffStatusPill } from "./pills";
import styles from "./Corrections.module.css";


// Drawer-level code splitting (speed): drawer chunks load
// on first open, not with the console route.
const AdjustmentRequestDrawer = dynamic(
  () => import("./AdjustmentRequestDrawer").then((m) => m.AdjustmentRequestDrawer),
  { ssr: false },
);
const AdjustmentDetailDrawer = dynamic(
  () => import("./AdjustmentDetailDrawer").then((m) => m.AdjustmentDetailDrawer),
  { ssr: false },
);
const FeeDrawer = dynamic(() => import("./FeeDrawer").then((m) => m.FeeDrawer), { ssr: false });
const WriteOffRequestDrawer = dynamic(
  () => import("./WriteOffRequestDrawer").then((m) => m.WriteOffRequestDrawer),
  { ssr: false },
);
const WriteOffDetailDrawer = dynamic(
  () => import("./WriteOffDetailDrawer").then((m) => m.WriteOffDetailDrawer),
  { ssr: false },
);
const RecoveryReceiptDialog = dynamic(
  () => import("./RecoveryReceiptDialog").then((m) => m.RecoveryReceiptDialog),
  { ssr: false },
);

type DrawerState =
  | null
  | { mode: "adjustment-request" }
  | { mode: "adjustment-detail"; adjustmentId: string }
  | { mode: "fee" }
  | { mode: "write-off-request" }
  | { mode: "write-off-detail"; writeOffId: string }
  | { mode: "receipt"; writeOffId: string };

export function CorrectionsScreen() {
  const permissions = usePermissions();
  const [drawer, setDrawer] = useState<DrawerState>(null);
  const [adjustmentLookup, setAdjustmentLookup] = useState("");
  const [adjustmentLookupError, setAdjustmentLookupError] = useState("");
  const [writeOffLookup, setWriteOffLookup] = useState("");
  const [writeOffLookupError, setWriteOffLookupError] = useState("");


  const mayCreate = can(permissions.data, "corrections", "create");

  // The two registers: server-ordered
  // keyset pages (pending-first / live-first) — never re-sorted or
  // filtered locally.
  const adjustments = useKeysetList<AdjustmentRecord>({
    queryKey: ["corrections", "adjustments-register"],
    fetchPage: (cursor) => fetchAdjustmentsPage(cursor),

  });
  const writeOffs = useKeysetList<WriteOffRecord>({
    queryKey: ["corrections", "write-offs-register"],
    fetchPage: (cursor) => fetchWriteOffsPage(cursor),

  });

  const adjustmentColumns: Column<AdjustmentRecord>[] = [
    {
      key: "status",
      header: "Status",
      render: (row) => adjustmentStatusPill(row.status),

    },
    {
      key: "adjustment",
      header: "Adjustment",
      render: (row) => (
        <span className={styles.mono} title={row.id}>
          {row.id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "loan",
      header: "Loan",
      render: (row) => (
        <span className={styles.mono} title={row.loan_id}>
          {row.loan_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "amount",
      header: "Amount",
      align: "right",
      // The SERVER's figure, verbatim (blocker (a)).
      render: (row) => <span className={styles.amountCell}>{fmtKes(row.amount)}</span>,
    },
    {
      key: "maker",
      header: "Maker",
      // Bare staff UUID under least disclosure (short-id render).
      render: (row) => (
        <span className={styles.mono} title={row.maker_id}>
          {row.maker_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "requested",
      header: "Requested",
      render: (row) => <span className={styles.muted}>{fmtDateTime(row.created_at)}</span>,
    },

  ];

  const writeOffColumns: Column<WriteOffRecord>[] = [
    {
      key: "status",
      header: "Status",
      render: (row) => writeOffStatusPill(row.status),

    },
    {
      key: "write_off",
      header: "Write-off",
      render: (row) => (
        <span className={styles.mono} title={row.id}>
          {row.id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "loan",
      header: "Loan",
      render: (row) => (
        <span className={styles.mono} title={row.loan_id}>
          {row.loan_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "member",
      header: "Member",
      render: (row) => (
        <span className={styles.mono} title={row.member_id}>
          {row.member_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "total",
      header: "Total written off",
      align: "right",
      // The write-once snapshot figure, verbatim — never summed.
      render: (row) => <span className={styles.amountCell}>{fmtKes(row.total_written_off)}</span>,
    },
    {
      key: "requested",
      header: "Requested",
      render: (row) => <span className={styles.muted}>{fmtDateTime(row.created_at)}</span>,
    },

  ];


  function openAdjustment() {
    const id = adjustmentLookup.trim();
    if (!isUuid(id)) {
      setAdjustmentLookupError("Enter the full adjustment UUID (8-4-4-4-12 hex).");
      return;
    }
    setAdjustmentLookupError("");
    setDrawer({ mode: "adjustment-detail", adjustmentId: id });
  }

  function openWriteOff() {
    const id = writeOffLookup.trim();
    if (!isUuid(id)) {
      setWriteOffLookupError("Enter the full write-off UUID (8-4-4-4-12 hex).");
      return;
    }
    setWriteOffLookupError("");
    setDrawer({ mode: "write-off-detail", writeOffId: id });
  }

  return (
    <div>
      <div className={grid.cards4}>
        <Card className={grid.half}>
          <div className={styles.cardTitle}>Repayment adjustments</div>
          <div className={styles.cardBody}>
            Two-phase maker-checker reversal of a repayment&apos;s COMPLETE
            allocation: a maker requests, a DIFFERENT checker approves the
            persisted snapshot — only then does the reversal post. No amounts
            are ever entered; every figure derives server-side from the
            original ledger legs.
          </div>
          <div className={styles.cardActions}>
            {mayCreate && (
              <Button
                type="button"
                variant="primary"
                onClick={() => setDrawer({ mode: "adjustment-request" })}
              >
                + Request adjustment
              </Button>
            )}
          </div>
          <div className={styles.lookupRow}>
            <div className={styles.lookupField}>
              <FormField
                id="adjustment-lookup"
                label="Review adjustment by id"
                error={adjustmentLookupError === "" ? undefined : adjustmentLookupError}
                hint="Fallback lookup (e.g. an id from the audit trail) — the checker register below lists pending requests first."
              >
                {(control) => (
                  <input
                    {...control}
                    className={styles.input}
                    value={adjustmentLookup}
                    onChange={(event) => setAdjustmentLookup(event.target.value)}
                    spellCheck={false}
                  />
                )}
              </FormField>
            </div>
            <Button type="button" onClick={openAdjustment}>
              Open adjustment
            </Button>
          </div>
        </Card>

        <Card className={grid.half}>
          <div className={styles.cardTitle}>Misc fees</div>
          <div className={styles.cardBody}>
            Post a tenant-configured fee against a member (FE- ledger ref).
            The fee AMOUNT is code-owned configuration resolved by the server
            — it cannot be entered here.
          </div>
          <div className={styles.cardActions}>
            {mayCreate && (
              <Button type="button" variant="primary" onClick={() => setDrawer({ mode: "fee" })}>
                + Post fee
              </Button>
            )}
            {!mayCreate && (
              <div className={styles.formNote}>
                Your role has no corrections create permission — posting
                affordances are not offered.
              </div>
            )}
          </div>
        </Card>

        <Card className={grid.wide}>
          <div className={styles.cardTitle}>Loan write-offs &amp; recoveries</div>
          <div className={styles.cardBody}>
            Committee-approved derecognition of an NPL loan bound to a
            write-once snapshot — write-off is NOT forgiveness: the legal claim
            survives, and recovery receipts are the only money-in
            path against it. Votes, void, posting and the recovery trail live
            on the record drawer.
          </div>
          <div className={styles.cardActions}>
            {mayCreate && (
              <Button
                type="button"
                variant="primary"
                onClick={() => setDrawer({ mode: "write-off-request" })}
              >
                + Request write-off
              </Button>
            )}
          </div>
          <div className={styles.lookupRow}>
            <div className={styles.lookupField}>
              <FormField
                id="write-off-lookup"
                label="Open write-off by id"
                error={writeOffLookupError === "" ? undefined : writeOffLookupError}
                hint="Fallback lookup (e.g. an id from the audit trail) — the committee register below lists live write-offs first."
              >
                {(control) => (
                  <input
                    {...control}
                    className={styles.input}
                    value={writeOffLookup}
                    onChange={(event) => setWriteOffLookup(event.target.value)}
                    spellCheck={false}
                  />
                )}
              </FormField>
            </div>
            <Button type="button" onClick={openWriteOff}>
              Open write-off
            </Button>
          </div>
        </Card>
      </div>

      <Card padded={false}>
        <div className={styles.registerHead}>
          <span>Pending-adjustments checker register</span>
          <span className={styles.registerNote}>
            Pending requests first, newest first — the server&apos;s
            order; figures are the persisted snapshot, verbatim
          </span>
        </div>
        <KeysetTable
          columns={adjustmentColumns}
          query={adjustments}
          rowKey={(row) => row.id}
          emptyMessage="No repayment adjustments yet — nothing awaits a checker."
          onRowClick={(row) => setDrawer({ mode: "adjustment-detail", adjustmentId: row.id })}
        />
      </Card>

      <Card padded={false}>
        <div className={styles.registerHead}>
          <span>Write-off committee register</span>
          <span className={styles.registerNote}>
            Live write-offs (awaiting votes or posting) first, newest first —
            the server&apos;s order; snapshot figures verbatim
          </span>
        </div>
        <KeysetTable
          columns={writeOffColumns}
          query={writeOffs}
          rowKey={(row) => row.id}
          emptyMessage="No write-offs yet — nothing awaits the committee."
          onRowClick={(row) => setDrawer({ mode: "write-off-detail", writeOffId: row.id })}
        />
      </Card>

      {drawer !== null && drawer.mode === "adjustment-request" && (
        <AdjustmentRequestDrawer
          onReview={(adjustmentId) => setDrawer({ mode: "adjustment-detail", adjustmentId })}
          onClose={() => setDrawer(null)}
        />
      )}
      {drawer !== null && drawer.mode === "adjustment-detail" && (
        <AdjustmentDetailDrawer
          adjustmentId={drawer.adjustmentId}
          onClose={() => setDrawer(null)}
        />
      )}
      {drawer !== null && drawer.mode === "fee" && <FeeDrawer onClose={() => setDrawer(null)} />}
      {drawer !== null && drawer.mode === "write-off-request" && (
        <WriteOffRequestDrawer
          onReview={(writeOffId) => setDrawer({ mode: "write-off-detail", writeOffId })}
          onClose={() => setDrawer(null)}
        />
      )}
      {drawer !== null && drawer.mode === "write-off-detail" && (
        <WriteOffDetailDrawer
          writeOffId={drawer.writeOffId}
          onRecordReceipt={() => setDrawer({ mode: "receipt", writeOffId: drawer.writeOffId })}
          onClose={() => setDrawer(null)}
        />
      )}
      {drawer !== null && drawer.mode === "receipt" && (
        <RecoveryReceiptDialog
          writeOffId={drawer.writeOffId}
          onClose={() => setDrawer(null)}
        />
      )}
    </div>
  );
}
