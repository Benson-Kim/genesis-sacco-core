"use client";

/**
 * Dormancy worklist — the console surface for the
 *  dormancy state.
 *
 * DISCOVERY HONESTY (recorded on): the EXISTING member register
 * contract already serves this worklist truthfully — `GET
 * /members?status=dormant` is the P8 status filter over the SERVER's
 * dormancy verdicts (the nightly job derives inactivity from the
 * ledger under row locks). NO contract change was needed and none was
 * made; no per-member "last activity" fact exists on the as-built
 * read contract, so none is rendered and activity is NEVER derived
 * client-side (honesty over decoration).
 *
 * - The worklist is the FLAT register read (identity facts; the
 *   aggregates expand is deliberately not opted into — a dormancy
 *   worklist needs WHO, not figures; blocker (a) satisfied
 *   structurally).
 * - `POST /members/jobs/dormancy` (members:edit — the job rewrites
 *   member status rows) surfaces behind a typed confirmation with
 *   verbatim result counts (the backfill precedent); the
 *   affordance is NOT rendered without the grant (pure UX — the server enforces regardless, least disclosure).
 * - Keyset pagination only (opaque cursors — scalability); every
 *   rendered string is attacker-influenced data rendered exclusively
 *   through React text interpolation.
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import { Button, Card, Pill } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { initials } from "@/lib/format";
import { fetchMembersPage, type MemberListFilters } from "../api";
import { STATUS_LABELS, type Member } from "../schemas";
import styles from "./Members.module.css";

// Drawer-level code splitting (speed): the dialog chunk
// loads on first open, not with the worklist route.
const DormancyRunDialog = dynamic(
    () => import("./DormancyRunDialog").then((m) => m.DormancyRunDialog),
    { ssr: false },
);

/** The worklist IS the register's dormant filter — pinned, not offered. */
const DORMANT_FILTERS: MemberListFilters = { status: "dormant", type: "" };

const COLUMNS: Column<Member>[] = [
    {
        key: "member",
        header: "Member",
        render: (member) => (
            <div className={styles.userCell}>
                <div className={styles.avatar} aria-hidden>
                    {initials(member.name)}
                </div>
                <div>
                    <div className={styles.cellStrong}>{member.name}</div>
                    <div className={styles.cellSub}>{member.member_no}</div>
                </div>
            </div>
        ),
    },
    {
        key: "contact",
        header: "Contact",
        render: (member) => (
            <div>
                <div>{member.phone ?? "—"}</div>
                <div className={styles.cellSub}>{member.email ?? "—"}</div>
            </div>
        ),
    },
    {
        key: "status",
        header: "Status",
        align: "right",
        // The SERVER's status verbatim (label map is a closed enum):
        // this screen never asserts or invents dormancy — a row is
        // here because the server's filter said so.
        render: (member) => <Pill bg="goldSoft" color="navy">{STATUS_LABELS[member.status]}</Pill>,
    },
];

export function DormancyWorklistScreen() {
    const permissions = usePermissions();
    const [dialogOpen, setDialogOpen] = useState(false);

    const list = useKeysetList<Member>({
        queryKey: ["members", "list", DORMANT_FILTERS],
        fetchPage: (cursor) => fetchMembersPage(DORMANT_FILTERS, cursor),
    });

    // The job rewrites member STATUS rows — members:edit (the manual
    // status route's power), never a generic operations right.
    const mayRunJob = can(permissions.data, "members", "edit");

    return (
        <div>
            <div className={styles.toolbar}>
                <p className={styles.panelNote}>
                    Members with no member-initiated ledger activity inside the
                    tenant-configured window, as judged by the SERVER&apos;s nightly
                    dormancy job (this list is the member register&apos;s own dormant
                    filter — no separate contract exists). Dormant members reactivate
                    automatically on their next deposit; the register serves the full
                    record.
                </p>
                {mayRunJob && (
                    <Button type="button" variant="primary" onClick={() => setDialogOpen(true)}>
                        Run dormancy scan…
                    </Button>
                )}
            </div>
            <Card padded={false}>
                <KeysetTable
                    columns={COLUMNS}
                    query={list}
                    rowKey={(member) => member.id}
                    emptyMessage="No dormant members — every account has recent activity."
                />
            </Card>
            {dialogOpen && <DormancyRunDialog onClose={() => setDialogOpen(false)} />}
        </div>
    );
}
