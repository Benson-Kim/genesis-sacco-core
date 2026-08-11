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
 *
 * Layout: the two registers (adjustments, write-offs) are separate TABS
 * (design-system TabList/TabPanel, reuse-first, the guarantors-screen
 * precedent) — each keyset hook mounts ONLY while its tab is active.
 * The catalogue/lookup cards above are not tables and stay outside the
 * tabs.
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import {
  Button,
  Card,
  KeysetTable,
  type Column,
  type TabDef,
  TabList,
  TabPanel,
  useKeysetList,
  useKeysetPagination,
} from "@genesis/design-system";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { getOwnUserId } from "@/modules/auth/session";
import {
  fmtAmount,
  fmtDateTime,
  fmtDateTimeParts,
  fmtKes,
} from "@/lib/format";
import { fetchAdjustmentsPage, fetchWriteOffsPage } from "../api";
import type { AdjustmentRecord, WriteOffRecord } from "../schemas";
import { adjustmentStatusPill, writeOffStatusPill } from "./pills";
import styles from "./Corrections.module.css";

// Drawer-level code splitting (speed): drawer chunks load
// on first open, not with the console route.
const AdjustmentRequestDrawer = dynamic(
  () =>
    import("./AdjustmentRequestDrawer").then((m) => m.AdjustmentRequestDrawer),
  { ssr: false },
);
const AdjustmentDetailDrawer = dynamic(
  () =>
    import("./AdjustmentDetailDrawer").then((m) => m.AdjustmentDetailDrawer),
  { ssr: false },
);
const FeeDrawer = dynamic(
  () => import("./FeeDrawer").then((m) => m.FeeDrawer),
  { ssr: false },
);
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

/**
 * Pending-adjustments checker register (tab 1). A separate component so
 * its keyset hook and pagination mount/reset ONLY while this tab is
 * active (the useKeysetList primitive is consumed unmodified, reuse-first).
 */
function AdjustmentsRegister({
  onOpen,
}: Readonly<{ onOpen: (adjustmentId: string) => void }>) {
  const pagination = useKeysetPagination();
  const adjustments = useKeysetList<AdjustmentRecord>({
    queryKey: ["corrections", "adjustments-register", pagination.pageSize],
    fetchPage: (cursor) => fetchAdjustmentsPage(cursor, pagination.pageSize),
  });

  const ownId = getOwnUserId();
  const columns: Column<AdjustmentRecord>[] = [
    {
      key: "status",
      header: "Status",
      render: (row) => adjustmentStatusPill(row.status),
    },
    {
      key: "member",
      header: "Member",
      // Identifier doctrine: number — name, resolved server-side on
      // the row. NEVER a uuid fallback — an unresolved leg renders
      // honest text, never a raw id.
      render: (row) =>
        row.member_no !== null ? (
          <div>
            <div className={styles.cellStrong}>{row.member_name}</div>
            <div className={styles.cellSub}>{row.member_no}</div>
          </div>
        ) : (
          <span className={styles.muted}>Unresolved member</span>
        ),
    },
    {
      key: "original",
      header: "Original posting",
      render: (row) =>
        row.original_txn_ref !== null ? (
          <span className={styles.mono}>{row.original_txn_ref}</span>
        ) : (
          <span className={styles.muted}>Not referenced</span>
        ),
    },
    {
      key: "amount",
      header: "Amount (KES)",
      align: "right",
      render: (row) => (
        <span className={styles.amountCell}>{fmtAmount(row.amount)}</span>
      ),
    },
    {
      key: "maker",
      header: "Requested by",
      // Least disclosure WITHOUT a raw staff uuid anywhere: only the
      // signed-in viewer's OWN request is distinguished; every other
      // maker renders as an honest, non-identifying label.
      render: (row) => (
        <span className={styles.muted}>
          {row.maker_id === ownId ? "You" : "Different officer"}
        </span>
      ),
    },
    {
      key: "requested",
      header: "Requested",
      render: (row) => {
        const { date, time } = fmtDateTimeParts(row.created_at);
        return (
          <div className={styles.dateTime}>
            <span className={styles.date}>{date}</span>
            {time && <span className={styles.time}>{time}</span>}
          </div>
        );
      },
    },
  ];

  return (
    <Card padded={false}>
      <KeysetTable
        columns={columns}
        query={adjustments}
        rowKey={(row) => row.id}
        emptyMessage="No repayment adjustments yet — nothing awaits a checker."
        onRowClick={(row) => onOpen(row.id)}
        pagination={{
          pageIndex: pagination.pageIndex,
          pageSize: pagination.pageSize,
          onPageChange: pagination.setPageIndex,
          onPageSizeChange: pagination.setPageSize,
          rowLabel: "adjustments",
        }}
      />
    </Card>
  );
}

/**
 * Write-off committee register (tab 2). A separate component so its
 * keyset hook and pagination mount/reset ONLY while this tab is active
 * (the useKeysetList primitive is consumed unmodified, reuse-first).
 */
function WriteOffsRegister({
  onOpen,
}: Readonly<{ onOpen: (writeOffId: string) => void }>) {
  const pagination = useKeysetPagination();
  const writeOffs = useKeysetList<WriteOffRecord>({
    queryKey: ["corrections", "write-offs-register", pagination.pageSize],
    fetchPage: (cursor) => fetchWriteOffsPage(cursor, pagination.pageSize),
  });

  const columns: Column<WriteOffRecord>[] = [
    {
      key: "status",
      header: "Status",
      render: (row) => writeOffStatusPill(row.status),
    },
    {
      key: "member",
      header: "Member",
      // Identifier doctrine: number — name, resolved server-side on
      // the row. NEVER a uuid fallback — an unresolved leg renders
      // honest text, never a raw id.
      render: (row) =>
        row.member_no !== null ? (
          <div>
            <div className={styles.cellStrong}>{row.member_name}</div>
            <div className={styles.cellSub}>{row.member_no}</div>
          </div>
        ) : (
          <span className={styles.muted}>Unresolved member</span>
        ),
    },
    {
      key: "total",
      header: "Total written off",
      align: "right",
      // The write-once snapshot figure, verbatim — never summed.
      render: (row) => (
        <span className={styles.amountCell}>
          {fmtKes(row.total_written_off)}
        </span>
      ),
    },
    {
      key: "requested",
      header: "Requested",
      render: (row) => (
        <span className={styles.muted}>{fmtDateTime(row.created_at)}</span>
      ),
    },
  ];

  return (
    <Card padded={false}>
      <KeysetTable
        columns={columns}
        query={writeOffs}
        rowKey={(row) => row.id}
        emptyMessage="No write-offs yet — nothing awaits the committee."
        onRowClick={(row) => onOpen(row.id)}
        pagination={{
          pageIndex: pagination.pageIndex,
          pageSize: pagination.pageSize,
          onPageChange: pagination.setPageIndex,
          onPageSizeChange: pagination.setPageSize,
          rowLabel: "write-offs",
        }}
      />
    </Card>
  );
}

type CorrectionsTabId = "adjustments" | "write-offs";

const TABS: TabDef[] = [
  { id: "adjustments", label: "Adjustments" },
  { id: "write-offs", label: "Write-offs" },
];

export function CorrectionsScreen() {
  const permissions = usePermissions();
  const [drawer, setDrawer] = useState<DrawerState>(null);
  const [activeTab, setActiveTab] = useState<CorrectionsTabId>("adjustments");

  const mayCreate = can(permissions.data, "corrections", "create");

  return (
    <div>
      {mayCreate && (
        <div className={styles.actions}>
          <Button
            type="button"
            variant="primary"
            onClick={() => setDrawer({ mode: "adjustment-request" })}
          >
            + Request adjustment
          </Button>

          <Button
            type="button"
            variant="primary"
            onClick={() => setDrawer({ mode: "fee" })}
          >
            + Post fee
          </Button>

          <Button
            type="button"
            variant="primary"
            onClick={() => setDrawer({ mode: "write-off-request" })}
          >
            + Request write-off
          </Button>
        </div>
      )}

      <div className={styles.registerTabs}>
        <TabList
          idPrefix="corrections"
          tabs={TABS}
          activeId={activeTab}
          onChange={(next) => setActiveTab(next as CorrectionsTabId)}
          ariaLabel="Correction registers"
        />
      </div>

      <TabPanel idPrefix="corrections" activeTabId={activeTab}>
        {activeTab === "adjustments" && (
          <AdjustmentsRegister
            onOpen={(adjustmentId) =>
              setDrawer({ mode: "adjustment-detail", adjustmentId })
            }
          />
        )}
        {activeTab === "write-offs" && (
          <WriteOffsRegister
            onOpen={(writeOffId) =>
              setDrawer({ mode: "write-off-detail", writeOffId })
            }
          />
        )}
      </TabPanel>

      {drawer !== null && drawer.mode === "adjustment-request" && (
        <AdjustmentRequestDrawer
          onReview={(adjustmentId) =>
            setDrawer({ mode: "adjustment-detail", adjustmentId })
          }
          onClose={() => setDrawer(null)}
        />
      )}
      {drawer !== null && drawer.mode === "adjustment-detail" && (
        <AdjustmentDetailDrawer
          adjustmentId={drawer.adjustmentId}
          onClose={() => setDrawer(null)}
        />
      )}
      {drawer !== null && drawer.mode === "fee" && (
        <FeeDrawer onClose={() => setDrawer(null)} />
      )}
      {drawer !== null && drawer.mode === "write-off-request" && (
        <WriteOffRequestDrawer
          onReview={(writeOffId) =>
            setDrawer({ mode: "write-off-detail", writeOffId })
          }
          onClose={() => setDrawer(null)}
        />
      )}
      {drawer !== null && drawer.mode === "write-off-detail" && (
        <WriteOffDetailDrawer
          writeOffId={drawer.writeOffId}
          onRecordReceipt={() =>
            setDrawer({ mode: "receipt", writeOffId: drawer.writeOffId })
          }
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
