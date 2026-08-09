"use client";

/**
 * Transactions ledger register (prototype `vTxn`).
 *
 * Security posture (loans/applications precedent):
 * - Every rendered string (txn refs, ids) is attacker-influenced data;
 *   it renders exclusively through React text interpolation — no
 *   parser sink exists in this module (gate-tested).
 * - UI affordances follow the P4 matrix via /me/permissions — pure UX;
 *   the server enforces every call (least disclosure). "Post transaction"
 *   mounts only with transactions:create AND members:view (the drawer
 *   cannot pick or fresh-read the member without the members grant —
 *   hidden, and ZERO member fetches otherwise). "Run deposit interest"
 *   mounts only with transactions:edit. The member filter mounts only
 *   with members:view.
 * - Keyset pagination only (opaque cursors — scalability); every filter
 *   (member, type, channel, direction, exact ref, date range) is a
 *   SERVER query parameter — nothing is filtered locally.
 * - MONEY (blocker (a)): each amount is an API decimal STRING rendered
 *   via fmtKes in the debit OR credit column per the SERVER-reported
 *   direction. Debits and credits are never netted or summed — the
 *   prototype's client-computed totals row is deliberately NOT
 *   reproduced (no API endpoint serves ledger totals; recorded as an
 *   honest limitation).
 */
import { useState, type FormEvent } from "react";
import dynamic from "next/dynamic";
import { Button, Card } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { fmtDateTime, fmtKes } from "@/lib/format";
import { FormField } from "@/modules/forms/FormField";
import { fetchMembersPage } from "@/modules/members/api";
import type { Member } from "@/modules/members/schemas";
import { EMPTY_TXN_FILTERS, fetchTransactionsPage, type TxnListFilters } from "../api";
import {
  CHANNELS,
  CHANNEL_LABELS,
  DATE_RE,
  SIDES,
  SIDE_LABELS,
  TXN_TYPES,
  TXN_TYPE_LABELS,
  type Transaction,
} from "../schemas";
import { reversalPill, txnTypePill } from "./pills";
import styles from "./Transactions.module.css";

// Drawer-level code splitting: drawer chunks load
// on first open, not with the list route.
const TransactionDetailDrawer = dynamic(
  () => import("./TransactionDetailDrawer").then((m) => m.TransactionDetailDrawer),
  { ssr: false },
);
const PostTransactionDrawer = dynamic(
  () => import("./PostTransactionDrawer").then((m) => m.PostTransactionDrawer),
  { ssr: false },
);
const InterestRunDialog = dynamic(
  () => import("./InterestRunDialog").then((m) => m.InterestRunDialog),
  { ssr: false },
);

type DrawerState =
  | null
  | { mode: "detail"; txn: Transaction }
  | { mode: "post" }
  | { mode: "interest" };

/**
 * Member filter — a separate component so its keyset hook mounts ONLY
 * for operators holding members:view (a stripped role fetches NOTHING;
 * the useKeysetList primitive is consumed unmodified, reuse-first).
 */
function MemberFilter({
  value,
  onChange,
}: Readonly<{
  value: string;
  onChange: (memberId: string) => void;
}>) {
  const members = useKeysetList<Member>({
    queryKey: ["members", "list", { status: "", type: "" }],
    fetchPage: (cursor) => fetchMembersPage({ status: "", type: "" }, cursor),
  });
  const options = members.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <div className={styles.filterGroup}>
      <FormField id="txn-filter-member" label="Member">
        {(control) => (
          <select
            {...control}
            className={`${styles.select} ${styles.filterControl}`}
            value={value}
            onChange={(event) => onChange(event.target.value)}
          >
            <option value="">All members</option>
            {options.map((member) => (
              <option key={member.id} value={member.id}>
                {member.name} · {member.member_no}
              </option>
            ))}
          </select>
        )}
      </FormField>
      {members.hasNextPage && (
        <Button
          type="button"
          onClick={() => void members.fetchNextPage()}
          disabled={members.isFetchingNextPage}
        >
          {members.isFetchingNextPage ? "Loading…" : "Load more members"}
        </Button>
      )}
    </div>
  );
}

export function TransactionsScreen() {
  const permissions = usePermissions();
  const [filters, setFilters] = useState<TxnListFilters>(EMPTY_TXN_FILTERS);
  // Text/date filters stage locally and apply on submit (one server
  // round-trip per applied filter set, not per keystroke).
  const [refDraft, setRefDraft] = useState("");
  // Free-text search (ref prefix / member number / name
  // prefix) — a SERVER query parameter, staged and applied like the
  // other drafts; nothing is filtered locally.
  const [searchDraft, setSearchDraft] = useState("");
  const [fromDraft, setFromDraft] = useState("");
  const [toDraft, setToDraft] = useState("");
  const [draftError, setDraftError] = useState("");
  // Date presets. Presets are a CLIENT-SIDE convenience
  // that compute the two existing declared params (date_from/date_to);
  // the server never sees a preset token. "" (All) sends no date keys.
  const [datePreset, setDatePreset] = useState<"" | "today" | "7d" | "30d" | "custom">("");
  const [drawer, setDrawer] = useState<DrawerState>(null);

  const list = useKeysetList<Transaction>({
    queryKey: ["transactions", "list", filters],
    fetchPage: (cursor) => fetchTransactionsPage(filters, cursor),
  });

  const mayViewMembers = can(permissions.data, "members", "view");
  // The post drawer requires the members grant to pick and FRESH-READ
  // the member before the money write (pattern (e)) — without it the
  // affordance is hidden, not disabled.
  const mayPost = can(permissions.data, "transactions", "create") && mayViewMembers;
  const mayRunInterest = can(permissions.data, "transactions", "edit");

  function applyDrafts(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // Manual entry can bypass type="date" in some browsers (review T4)
    // — validate the ISO shape before it becomes a server query param.
    const fromValue = fromDraft.trim();
    const toValue = toDraft.trim();
    if (
      (fromValue !== "" && !DATE_RE.test(fromValue)) ||
      (toValue !== "" && !DATE_RE.test(toValue))
    ) {
      setDraftError("Enter dates as YYYY-MM-DD.");
      return;
    }
    setDraftError("");
    // Manual dates supersede any preset: the pressed state moves to
    // Custom (or All when both are empty) so the UI never lies.
    setDatePreset(fromValue === "" && toValue === "" ? "" : "custom");
    setFilters((current) => ({
      ...current,
      ref: refDraft.trim(),
      search: searchDraft.trim(),
      date_from: fromValue,
      date_to: toValue,
    }));
  }

  function applyDatePreset(preset: "" | "today" | "7d" | "30d" | "custom") {
    setDatePreset(preset);
    if (preset === "custom") return; // manual from/to inputs take over
    if (preset === "") {
      setFromDraft("");
      setToDraft("");
      setFilters((current) => ({ ...current, date_from: "", date_to: "" }));
      return;
    }
    // Hand-computable window: [today - (N-1) days, today], local clock.
    const spanDays = preset === "today" ? 1 : preset === "7d" ? 7 : 30;
    const to = new Date();
    const from = new Date(to);
    from.setDate(to.getDate() - (spanDays - 1));
    const iso = (d: Date) =>
      `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const fromValue = iso(from);
    const toValue = iso(to);
    setFromDraft(fromValue);
    setToDraft(toValue);
    setDraftError("");
    setFilters((current) => ({ ...current, date_from: fromValue, date_to: toValue }));
  }

  const columns: Column<Transaction>[] = [
    {
      key: "date",
      header: "Date",
      render: (txn) => <span className={styles.muted}>{fmtDateTime(txn.occurred_at)}</span>,
    },
    {
      key: "ref",
      header: "Ref",
      // external_ref: the operator-entered external
      // receipt reference, muted next to the system ref; attacker-
      // influenced data — React text interpolation only.
      render: (txn) => (
        <span className={styles.mono}>
          {txn.txn_ref}
          {txn.external_ref !== null && (
            <span className={styles.muted}> · {txn.external_ref}</span>
          )}
        </span>
      ),
    },
    {
      key: "member",
      header: "Member",
      render: (txn) =>
        // The P11 list carries member_id only (no joined name) — the
        // detail drawer resolves the member record.
        txn.member_id === null ? (
          <span className={styles.muted}>—</span>
        ) : (
          <span className={styles.mono} title={txn.member_id}>
            {txn.member_id.slice(0, 8)}
          </span>
        ),
    },
    {
      key: "type",
      header: "Type",
      render: (txn) => (
        <>
          {txnTypePill(txn.type, txn.direction)} {txn.is_reversal && reversalPill()}
        </>
      ),
    },
    {
      key: "channel",
      header: "Channel",
      render: (txn) => <span className={styles.muted}>{CHANNEL_LABELS[txn.channel]}</span>,
    },
    {
      key: "debit",
      header: "Debit",
      align: "right",
      render: (txn) =>
        // The SERVER-reported direction places the amount; the other
        // column stays em-dash. Never netted, never summed (blocker (a)).
        txn.direction === "debit" ? (
          <span className={styles.drCell}>{fmtKes(txn.amount)}</span>
        ) : (
          <span className={styles.emptyCell}>—</span>
        ),
    },
    {
      key: "credit",
      header: "Credit",
      align: "right",
      render: (txn) =>
        txn.direction === "credit" ? (
          <span className={styles.crCell}>{fmtKes(txn.amount)}</span>
        ) : (
          <span className={styles.emptyCell}>—</span>
        ),
    },
  ];

  return (
    <div>
      <div className={styles.toolbar}>
        <div className={styles.filters}>
          <FormField id="txn-filter-type" label="Type">
            {(control) => (
              <select
                {...control}
                className={`${styles.select} ${styles.filterControl}`}
                value={filters.type}
                onChange={(event) =>
                  setFilters((current) => ({
                    ...current,
                    type: event.target.value as TxnListFilters["type"],
                  }))
                }
              >
                <option value="">All types</option>
                {TXN_TYPES.map((option) => (
                  <option key={option} value={option}>
                    {TXN_TYPE_LABELS[option]}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <FormField id="txn-filter-channel" label="Channel">
            {(control) => (
              <select
                {...control}
                className={`${styles.select} ${styles.filterControl}`}
                value={filters.channel}
                onChange={(event) =>
                  setFilters((current) => ({
                    ...current,
                    channel: event.target.value as TxnListFilters["channel"],
                  }))
                }
              >
                <option value="">All channels</option>
                {CHANNELS.map((option) => (
                  <option key={option} value={option}>
                    {CHANNEL_LABELS[option]}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <div className={styles.filterGroup}>
            <span className={styles.filterLabel}>Direction</span>
            <div className={styles.segment} role="group" aria-label="Direction">
              <button
                type="button"
                className={styles.segmentButton}
                aria-pressed={filters.direction === ""}
                onClick={() => setFilters((current) => ({ ...current, direction: "" }))}
              >
                All
              </button>
              {SIDES.map((option) => (
                <button
                  key={option}
                  type="button"
                  className={styles.segmentButton}
                  aria-pressed={filters.direction === option}
                  onClick={() => setFilters((current) => ({ ...current, direction: option }))}
                >
                  {SIDE_LABELS[option]}
                </button>
              ))}
            </div>
          </div>
          {mayViewMembers && (
            <MemberFilter
              value={filters.member_id}
              onChange={(memberId) =>
                setFilters((current) => ({ ...current, member_id: memberId }))
              }
            />
          )}
          <div className={styles.filterGroup}>
            <span className={styles.filterLabel}>Date</span>
            <div className={styles.segment} role="group" aria-label="Date preset">
              {(
                [
                  ["", "All"],
                  ["today", "Today"],
                  ["7d", "Last 7 days"],
                  ["30d", "Last 30 days"],
                  ["custom", "Custom range"],
                ] as const
              ).map(([preset, label]) => (
                <button
                  key={label}
                  type="button"
                  className={styles.segmentButton}
                  aria-pressed={datePreset === preset}
                  onClick={() => applyDatePreset(preset)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <form className={styles.filters} onSubmit={applyDrafts} noValidate>
            <FormField id="txn-filter-search" label="Search (ref or member)">
              {(control) => (
                <input
                  {...control}
                inputMode="search"
                  className={`${styles.input} ${styles.filterControl}`}
                  maxLength={64}
                  value={searchDraft}
                  onChange={(event) => setSearchDraft(event.target.value)}
                />
              )}
            </FormField>
            <FormField id="txn-filter-ref" label="Reference (exact)">
              {(control) => (
                <input
                  {...control}
                inputMode="search"
                  className={`${styles.input} ${styles.filterControl}`}
                  maxLength={32}
                  value={refDraft}
                  onChange={(event) => setRefDraft(event.target.value)}
                />
              )}
            </FormField>
            <FormField id="txn-filter-from" label="From date">
              {(control) => (
                <input
                  {...control}
                type="date"
                  className={`${styles.input} ${styles.filterControl}`}
                  value={fromDraft}
                  onChange={(event) => setFromDraft(event.target.value)}
                />
              )}
            </FormField>
            <FormField id="txn-filter-to" label="To date">
              {(control) => (
                <input
                  {...control}
                type="date"
                  className={`${styles.input} ${styles.filterControl}`}
                  value={toDraft}
                  onChange={(event) => setToDraft(event.target.value)}
                />
              )}
            </FormField>
            <Button type="submit">Apply</Button>
            {draftError !== "" && (
              <span className={styles.formNote} role="alert">
                {draftError}
              </span>
            )}
          </form>
        </div>
        <div className={styles.toolbarActions}>
          {mayRunInterest && (
            <Button type="button" onClick={() => setDrawer({ mode: "interest" })}>
              Run deposit interest
            </Button>
          )}
          {mayPost && (
            <Button
              type="button"
              variant="primary"
              onClick={() => setDrawer({ mode: "post" })}
            >
              + Post transaction
            </Button>
          )}
        </div>
      </div>

      <Card padded={false}>
        <div className={styles.ledgerHead}>
          <span>Transactions ledger</span>
          <span className={styles.ledgerNote}>
            Append-only server ledger — amounts render verbatim; totals are
            not computed in this screen
          </span>
        </div>
        <KeysetTable
          columns={columns}
          query={list}
          rowKey={(txn) => txn.id}
          emptyMessage="No transactions match this filter."
          onRowClick={(txn) => setDrawer({ mode: "detail", txn })}
        />
      </Card>

      {drawer !== null && drawer.mode === "detail" && (
        <TransactionDetailDrawer txn={drawer.txn} onClose={() => setDrawer(null)} />
      )}
      {drawer !== null && drawer.mode === "post" && (
        <PostTransactionDrawer onClose={() => setDrawer(null)} />
      )}
      {drawer !== null && drawer.mode === "interest" && (
        <InterestRunDialog onClose={() => setDrawer(null)} />
      )}
    </div>
  );
}
