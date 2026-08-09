"use client";

/**
 * Generic branch roster panel — ONE component
 * consumed for both roster kinds (reuse-first, the BranchAssignPanel precedent): the expand-only keyset reads over the 0016 assignment
 * columns, rendered inside the branch detail drawer.
 *
 * Permission model (the assignment split MIRRORED on reads, enforced server-side regardless — least disclosure): the USER roster mounts
 * only with access_control:view, the MEMBER roster only with
 * members:view. The DRAWER decides mounting — this panel is only ever
 * rendered under its grant, so withholding is STRUCTURAL: an
 * unmounted panel holds no query and probes nothing (jest-proven).
 *
 * - Keyset pagination only (opaque cursors — scalability) through the
 *   shared useKeysetList/KeysetTable primitives (reuse-first).
 * - Identity facts VERBATIM: names/emails are operator free text —
 *   attacker-influenced data rendered exclusively as React text (the
 *   module's named XSS threat, gate-tested). NO money field exists on
 *   either roster contract and none is asserted or invented.
 */
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import type { KeysetPage } from "@/modules/table/schemas";
import styles from "./Branches.module.css";

export interface RosterAdapter<T> {
  /** Query-key discriminator ("users" | "members"). */
  kind: string;
  title: string;
  /** Permission honesty line rendered under the roster. */
  note: string;
  columns: Column<T>[];
  fetchPage: (branchId: string, cursor: string | null) => Promise<KeysetPage<T>>;
  rowKey: (row: T) => string;
  emptyMessage: string;
}

export function BranchRosterPanel<T>({
  branchId,
  adapter,
}: Readonly<{
  branchId: string;
  adapter: RosterAdapter<T>;
}>) {
  const list = useKeysetList<T>({
    queryKey: ["branches", "roster", adapter.kind, branchId],
    fetchPage: (cursor) => adapter.fetchPage(branchId, cursor),
  });

  return (
    <div>
      <div className={styles.subhead}>{adapter.title}</div>
      <KeysetTable
        columns={adapter.columns}
        query={list}
        rowKey={adapter.rowKey}
        emptyMessage={adapter.emptyMessage}
      />
      <div className={styles.formNote}>{adapter.note}</div>
    </div>
  );
}
