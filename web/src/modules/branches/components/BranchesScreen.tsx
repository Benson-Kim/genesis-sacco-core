"use client";

/**
 * Branches register (the registry console):
 * the organisational-registry read surface plus entry points for the
 * registry writes (register, rename, assignments, the legacy
 * backfill).
 *
 * Security posture (settings/dividends precedent):
 * - Every rendered string (ids, names, timestamps) is treated as
 *   attacker-influenced data; it renders exclusively through React
 *   text interpolation — no parser sink exists in this module
 *   (gate-tested; branch NAMES are operator free text and the named
 *   XSS threat here).
 * - UI affordances follow the P4 matrix via /me/permissions — pure UX;
 *   the server enforces every call (least disclosure). The route itself is
 *   settings:view; "+ Register branch" mounts with settings:create,
 *   the backfill with settings:edit; rename/assignment affordances
 *   mount in the drawer per THEIR OWN modules (see api.ts — the
 *   registry/entity permission split recorded on the router).
 * - Keyset pagination only (opaque cursors — scalability); the list
 *   contract declares NO filters — none are offered and nothing is
 *   filtered locally.
 * - NO money figure exists on this contract — nothing is summed,
 *   formatted or derived here (blocker (a) satisfied structurally).
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import { Button, Card } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { fmtDateTime } from "@/lib/format";
import { fetchBranchesPage } from "../api";
import type { BranchRecord } from "../schemas";
import styles from "./Branches.module.css";

// Drawer-level code splitting (speed): drawer chunks load
// on first open, not with the list route.
const BranchCreateDrawer = dynamic(
  () => import("./BranchCreateDrawer").then((m) => m.BranchCreateDrawer),
  { ssr: false },
);
const BranchDetailDrawer = dynamic(
  () => import("./BranchDetailDrawer").then((m) => m.BranchDetailDrawer),
  { ssr: false },
);
const BackfillDialog = dynamic(() => import("./BackfillDialog").then((m) => m.BackfillDialog), {
  ssr: false,
});

type DrawerState =
  | null
  | { mode: "create" }
  | { mode: "detail"; branchId: string }
  | { mode: "backfill" };

export function BranchesScreen() {
  const permissions = usePermissions();
  const [drawer, setDrawer] = useState<DrawerState>(null);

  const list = useKeysetList<BranchRecord>({
    queryKey: ["branches", "list"],
    fetchPage: (cursor) => fetchBranchesPage(cursor),
  });

  const mayCreate = can(permissions.data, "settings", "create");
  const mayEdit = can(permissions.data, "settings", "edit");

  const columns: Column<BranchRecord>[] = [
    {
      key: "name",
      header: "Branch",
      render: (branch) => <span>{branch.name}</span>,
    },
    {
      key: "created",
      header: "Registered",
      render: (branch) => <span className={styles.muted}>{fmtDateTime(branch.created_at)}</span>,
    },
    {
      key: "id",
      header: "Record",
      render: (branch) => (
        <span className={styles.mono} title={branch.id}>
          {branch.id.slice(0, 8)}
        </span>
      ),
    },
  ];

  return (
    <div>
      <div className={styles.toolbar}>
        <div className={styles.toolbarNote}>
          Branches are organisational metadata — no money
          value exists on a branch record. Assigning PEOPLE to a branch is a
          user-administration or member-edit action with its own permission,
          never a settings right (the registry/entity split, server-enforced).
        </div>
        <div className={styles.toolbarActions}>
          {mayEdit && (
            <Button type="button" onClick={() => setDrawer({ mode: "backfill" })}>
              Run legacy backfill…
            </Button>
          )}
          {mayCreate && (
            <Button type="button" variant="primary" onClick={() => setDrawer({ mode: "create" })}>
              + Register branch
            </Button>
          )}
        </div>
      </div>

      <Card padded={false}>
        <div className={styles.registerHead}>
          <span>Branch registry</span>
          <span className={styles.registerNote}>Newest first · server records verbatim</span>
        </div>
        <KeysetTable
          columns={columns}
          query={list}
          rowKey={(branch) => branch.id}
          emptyMessage="No branches registered yet."
          onRowClick={(branch) => setDrawer({ mode: "detail", branchId: branch.id })}
        />
      </Card>

      {drawer !== null && drawer.mode === "create" && (
        <BranchCreateDrawer onClose={() => setDrawer(null)} />
      )}
      {drawer !== null && drawer.mode === "detail" && (
        <BranchDetailDrawer branchId={drawer.branchId} onClose={() => setDrawer(null)} />
      )}
      {drawer !== null && drawer.mode === "backfill" && (
        <BackfillDialog onClose={() => setDrawer(null)} />
      )}
    </div>
  );
}
