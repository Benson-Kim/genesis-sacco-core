"use client";

/**
 * Share-transfers console (the human-authorized maker-checker rework of the (e) console). Consumes EXACTLY the reworked contract:
 *
 * - MAKER phase `POST /members/{member_id}/share-transfers`
 *   (members:approve): creates a PENDING workflow record bound to the
 *   persisted approval snapshot — NO money moves from this form. It
 *   lives in its OWN modal (RequestTransferDrawer, opened by the
 *   "Transfer shares…" button) — never inline on this screen.
 * - The ledger-(m) history REGISTER `GET /share-transfers`
 *   (members:view): keyset, PENDING FIRST then newest first — the
 *   server's order, never re-sorted locally. A row opens the review
 *   drawer where a DIFFERENT principal approves or rejects
 *   (TransferDetailDrawer — the checker surface).
 *
 * Security posture (the corrections precedent):
 * - Route guard is members:view (RequireModule); the request modal
 *   mounts ONLY with members:approve — pure UX; the server enforces
 *   every call (least disclosure, deny by default). Checker affordances live
 *   in the drawer behind MakerCheckerPanel (structural SoD withhold).
 * - MONEY (blocker (a)): the amount is the operation's SUBJECT — the
 *   typed decimal STRING end-to-end, never a float. Every rendered
 *   figure is the SERVER's, verbatim.
 * - LEAST DISCLOSURE / NO RAW UUID, EVER: members are identified and
 *   searched exclusively by member number or national ID (the
 *   RequestTransferDrawer lookup) and rendered by member_no/name; this
 *   screen never shows a transfer's own id or a staff (maker) id — a
 *   bare identifier fragment is not actionable to an operator, so
 *   neither the register nor the drawers surface one.
 * - Every rendered string is attacker-influenced data; it renders
 *   exclusively through React text interpolation (gate-tested).
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import {
  Button,
  Card,
  KeysetTable,
  type Column,
  useKeysetPagination,
  useKeysetList,
} from "@genesis/design-system";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { getOwnUserId } from "@/modules/auth/session";
import { fmtAmount, fmtDateTimeParts } from "@/lib/format";
import { fetchShareTransfersPage } from "../api";
import type { ShareTransferRecord } from "../schemas";
import { transferStatusPill } from "./pills";
import styles from "./Shares.module.css";

// Drawer-level code splitting (speed): drawer chunks load on
// first open, not with the console route.
const RequestTransferDrawer = dynamic(
  () => import("./RequestTransferDrawer").then((m) => m.RequestTransferDrawer),
  { ssr: false },
);
const TransferDetailDrawer = dynamic(
  () => import("./TransferDetailDrawer").then((m) => m.TransferDetailDrawer),
  { ssr: false },
);

export function ShareTransfersScreen() {
  const permissions = usePermissions();
  const [openTransferId, setOpenTransferId] = useState<string | null>(null);
  const [requesting, setRequesting] = useState(false);

  // The ledger-(m) register: server-ordered keyset pages (pending
  // first — the checker's job order) — never re-sorted or filtered
  // locally.
  const pagination = useKeysetPagination();
  const register = useKeysetList<ShareTransferRecord>({
    queryKey: ["shares", "transfers-register", pagination.pageSize],
    fetchPage: (cursor) => fetchShareTransfersPage(cursor, pagination.pageSize),
  });

  const mayRequest = can(permissions.data, "members", "approve");
  const ownId = getOwnUserId();

  const registerColumns: Column<ShareTransferRecord>[] = [
    {
      key: "status",
      header: "Status",
      render: (row) => transferStatusPill(row.status),
    },
    {
      key: "from",
      header: "From member",
      render: (row) =>
        // Identifier doctrine: number — name, resolved server-side on
        // the row. NEVER a uuid fallback — an unresolved leg (should
        // not happen; the server resolves both in the same statement)
        // renders honest text, never a raw id.
        row.from_member_no !== null ? (
          <div>
            <div className={styles.cellStrong}>{row.from_member_name}</div>
            <div className={styles.cellSub}>{row.from_member_no}</div>
          </div>
        ) : (
          <span className={styles.muted}>Unresolved member</span>
        ),
    },
    {
      key: "to",
      header: "To member",
      render: (row) =>
        row.to_member_no !== null ? (
          <div>
            <div className={styles.cellStrong}>{row.to_member_name}</div>
            <div className={styles.cellSub}>{row.to_member_no}</div>
          </div>
        ) : (
          <span className={styles.muted}>Unresolved member</span>
        ),
    },
    {
      key: "amount",
      header: "Amount (KES)",
      align: "right",
      render: (row) => <span className={styles.amountCell}>{fmtAmount(row.amount)}</span>,
    },
    {
      key: "maker",
      header: "Requested by",
      // Least disclosure WITHOUT a raw staff uuid anywhere: only the
      // signed-in viewer's OWN request is distinguished; every other
      // maker renders as an honest, non-identifying label (the
      // TransferDetailDrawer makerLabel convention, applied here too).
      render: (row) => {
        if (row.created_by === null) return <span className={styles.muted}>—</span>;
        return (
          <span className={styles.muted}>
            {row.created_by === ownId ? "You" : "Different officer"}
          </span>
        );
      },
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
    <div>
      <div className={styles.cardActions}>
        {mayRequest && (
          <Button type="button" variant="primary" onClick={() => setRequesting(true)}>
            Transfer shares…
          </Button>
        )}
        {!mayRequest && (
          <div className={styles.formNote}>
            Your role has no members approval permission — share-transfer
            requests move member equity and their controls are not offered.
          </div>
        )}
      </div>

      <Card padded={false}>
        <KeysetTable
          query={register}
          columns={registerColumns}
          rowKey={(row) => row.id}
          onRowClick={(row) => setOpenTransferId(row.id)}
          emptyMessage="No share transfers exist for this SACCO yet."
          pagination={{
            pageIndex: pagination.pageIndex,
            pageSize: pagination.pageSize,
            onPageChange: pagination.setPageIndex,
            onPageSizeChange: pagination.setPageSize,
            rowLabel: "transfers",
          }}
        />
      </Card>

      {requesting && <RequestTransferDrawer onClose={() => setRequesting(false)} />}

      {openTransferId !== null && (
        <TransferDetailDrawer
          transferId={openTransferId}
          onClose={() => setOpenTransferId(null)}
        />
      )}
    </div>
  );
}
