"use client";

/**
 *  members module (P8 API): keyset member register with segmented
 * status/type filters and permission-gated registration.
 *
 * - UI affordances follow the P4 matrix via /me/permissions — pure UX;
 *   the server enforces every call (least disclosure). The register button is
 *   NOT rendered without can(create) — the UI never offers what the API
 *   forbids.
 * - Keyset pagination only (opaque cursors — scalability).
 * - The register opts in to the LIST aggregates (review: include=aggregates, one set-based server statement per page) and
 *   renders the four advisory figures VERBATIM — the server strings
 *   for deposits, shares, loans, guarantees are never summed, derived
 *   or re-formatted here (the money rule: no client-side money math).
 * - Every rendered string (names, emails, phones, figures) is
 *   attacker-influenced data and renders exclusively through React
 *   text interpolation.
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import { Banner, Button, Card, Pill } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { initials } from "@/lib/format";
import { fetchMembersPageWithAggregates, type MemberListFilters } from "../api";
import {
    MEMBER_STATUSES,
    MEMBER_TYPES,
    STATUS_LABELS,
    TYPE_LABELS,
    type Member,
    type MemberDetail,
    type MemberStatus,
    type MemberType,
} from "../schemas";
import styles from "./Members.module.css";

// Drawer-level code splitting (speed): the registration
// drawer chunk loads on first open, not with the register route.
const MemberCreateDrawer = dynamic(
    () => import("./MemberCreateDrawer").then((m) => m.MemberCreateDrawer),
    { ssr: false },
);
// KYC surfaces: row drill-down drawer + the
// registration wizard continuation after a quick-create.
const MemberKycDrawer = dynamic(
    () => import("./MemberKycDrawer").then((m) => m.MemberKycDrawer),
    { ssr: false },
);
const KycWizard = dynamic(() => import("./KycWizard").then((m) => m.KycWizard), {
    ssr: false,
});

function statusPill(status: MemberStatus) {
    switch (status) {
        case "active":
            return (
                <Pill bg="emeraldSoft" color="emerald">
                    {STATUS_LABELS.active}
                </Pill>
            );
        case "arrears":
            return (
                <Pill bg="orangeSoft" color="orange">
                    {STATUS_LABELS.arrears}
                </Pill>
            );
        case "dormant":
            return (
                <Pill bg="goldSoft" color="navy">
                    {STATUS_LABELS.dormant}
                </Pill>
            );
        case "exited":
            return (
                <Pill bg="brickSoft" color="brick">
                    {STATUS_LABELS.exited}
                </Pill>
            );
    }
}

const COLUMNS: Column<MemberDetail>[] = [
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
        key: "type",
        header: "Type",
        render: (member) => <Pill>{TYPE_LABELS[member.type]}</Pill>,
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
    // Advisory aggregates (review): SERVER decimal strings
    // rendered VERBATIM — never summed, never derived, never
    // re-formatted (the money rule; the same figures the KYC drawer
    // shows on the detail read).
    {
        key: "deposits",
        header: "Deposits",
        align: "right",
        render: (member) => member.aggregates.deposits_total,
    },
    {
        key: "shares",
        header: "Share capital",
        align: "right",
        render: (member) => member.aggregates.shares_total,
    },
    {
        key: "loans",
        header: "Loans outstanding",
        align: "right",
        render: (member) => member.aggregates.loans_outstanding,
    },
    {
        key: "guarantees",
        header: "Guarantees pledged",
        align: "right",
        render: (member) => member.aggregates.guarantees_pledged,
    },
    {
        key: "status",
        header: "Status",
        align: "right",
        render: (member) => statusPill(member.status),
    },
];

function SegmentedFilter<T extends string>({
    label,
    options,
    labels,
    value,
    onChange,
}: Readonly<{
    label: string;
    options: readonly T[];
    labels: Record<T, string>;
    value: T | "";
    onChange: (next: T | "") => void;
}>) {
    return (
        <div className={styles.filterGroup}>
            <span className={styles.filterLabel}>{label}</span>
            <div className={styles.segment} role="group" aria-label={label}>
                <button
                    type="button"
                    className={styles.segmentButton}
                    aria-pressed={value === ""}
                    onClick={() => onChange("")}
                >
                    All
                </button>
                {options.map((option) => (
                    <button
                        key={option}
                        type="button"
                        className={styles.segmentButton}
                        aria-pressed={value === option}
                        onClick={() => onChange(option)}
                    >
                        {labels[option]}
                    </button>
                ))}
            </div>
        </div>
    );
}

export function MembersScreen() {
    const permissions = usePermissions();
    const [status, setStatus] = useState<MemberStatus | "">("");
    const [type, setType] = useState<MemberType | "">("");
    const [drawerOpen, setDrawerOpen] = useState(false);
    const [createdNo, setCreatedNo] = useState<string | null>(null);
    // Row drill-down (KYC drawer) and the wizard continuation.
    const [kycMember, setKycMember] = useState<Member | null>(null);
    const [wizardMember, setWizardMember] = useState<Member | null>(null);

    const filters: MemberListFilters = { status, type };
    const list = useKeysetList<MemberDetail>({
        queryKey: ["members", "list", filters],
        fetchPage: (cursor) => fetchMembersPageWithAggregates(filters, cursor),
    });

    const mayCreate = can(permissions.data, "members", "create");

    return (
        <div>
            <div className={styles.toolbar}>
                <div className={styles.filters}>
                    <SegmentedFilter
                        label="Status"
                        options={MEMBER_STATUSES}
                        labels={STATUS_LABELS}
                        value={status}
                        onChange={setStatus}
                    />
                    <SegmentedFilter
                        label="Type"
                        options={MEMBER_TYPES}
                        labels={TYPE_LABELS}
                        value={type}
                        onChange={setType}
                    />
                </div>
                {mayCreate && (
                    <Button variant="primary" onClick={() => setDrawerOpen(true)}>
                        Register member
                    </Button>
                )}
            </div>
            {createdNo !== null && (
                <Banner variant="ok">Member {createdNo} registered.</Banner>
            )}
            <Card padded={false}>
                <KeysetTable
                    columns={COLUMNS}
                    query={list}
                    rowKey={(member) => member.id}
                    emptyMessage="No members match these filters."
                    onRowClick={(member) => setKycMember(member)}
                />
            </Card>
            {drawerOpen && (
                <MemberCreateDrawer
                    onClose={() => setDrawerOpen(false)}
                    onCreated={(member) => {
                        setDrawerOpen(false);
                        setCreatedNo(member.member_no);
                        // Prototype wizard continuation: identity
                        // captured — proceed to the
                        // KYC profile + documents steps.
                        setWizardMember(member);
                    }}
                />
            )}
            {kycMember !== null && (
                <MemberKycDrawer
                    member={kycMember}
                    onStartWizard={() => {
                        setWizardMember(kycMember);
                        setKycMember(null);
                    }}
                    onClose={() => setKycMember(null)}
                />
            )}
            {wizardMember !== null && (
                <KycWizard member={wizardMember} onClose={() => setWizardMember(null)} />
            )}
        </div>
    );
}

