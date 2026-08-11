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
import { useEffect, useRef, useState, type FormEvent } from "react";
import dynamic from "next/dynamic";
import {
  Button,
  Card,
  FilterControl,
  SelectFilter,
  KeysetTable,
  type Column,
  useKeysetList,
  useKeysetPagination,
  Input,
} from "@genesis/design-system";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { fmtAmount, fmtDateTimeParts } from "@/lib/format";
import type { ExportFilterDraft } from "@/modules/reports/schemas";
import { EMPTY_TXN_FILTERS, fetchTransactionsPage, type TxnListFilters } from "../api";
import {
  CHANNELS,
  CHANNEL_LABELS,
  DATE_RE,
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
const RequestExportDrawer = dynamic(
  () =>
    import("@/modules/reports/components/RequestExportDrawer").then(
      (m) => m.RequestExportDrawer,
    ),
  { ssr: false },
);

type DrawerState =
  | null
  | { mode: "detail"; txn: Transaction }
  | { mode: "post" }
  | { mode: "interest" }
  | { mode: "export" };

/**
 * The register page's ACTIVE filters as the export drawer's pre-fill
 * (#35 item 5): a pure key rename (type -> txn_type; the rest map
 * 1:1), empty strings dropped — the SAME values the list request is
 * currently using, so the export scope matches the visible register.
 * Hand-computable: {type:"deposit", channel:"mpesa"} becomes
 * {txn_type:"deposit", channel:"mpesa"}.
 */
export function exportDraftFromFilters(filters: TxnListFilters): Partial<ExportFilterDraft> {
  const entries: [keyof ExportFilterDraft, string][] = [
    ["member_id", filters.member_id],
    ["txn_type", filters.type],
    ["channel", filters.channel],
    ["direction", filters.direction],
    ["ref", filters.ref],
    ["search", filters.search],
    ["date_from", filters.date_from],
    ["date_to", filters.date_to],
  ];
  const draft: Partial<ExportFilterDraft> = {};
  for (const [key, value] of entries) {
    if (value !== "") draft[key] = value;
  }
  return draft;
}


/**
 * Hand-computable preset window: [today - (N-1) days, today] on the
 * local clock. Module-level so the MOUNT default and the change handler
 * derive the window from ONE rule — the selected preset and the filters
 * actually sent can never disagree.
 */
function presetWindow(preset: "today" | "7d" | "30d"): { from: string; to: string } {
  const spanDays = preset === "today" ? 1 : preset === "7d" ? 7 : 30;
  const to = new Date();
  const from = new Date(to);
  from.setDate(to.getDate() - (spanDays - 1));
  const iso = (d: Date) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return { from: iso(from), to: iso(to) };
}

export function TransactionsScreen() {
  const permissions = usePermissions();

  // The default "today" window, from the SAME rule the change handler
  // uses — so the selected preset and the filters actually sent agree.
  const todayIso = presetWindow("today").to;

  // Default to today's date range so the list opens on current day's activity.
  const [filters, setFiltersRaw] = useState<TxnListFilters>({
    ...EMPTY_TXN_FILTERS,
    date_from: todayIso,
    date_to: todayIso,
  });
  const pagination = useKeysetPagination();

  // Filter changes restart from page 0 (the fetch starts a new keyset walk).
  const setFilters: typeof setFiltersRaw = (action) => {
    setFiltersRaw(action);
    pagination.setPageIndex(0);
  };
  // Text/date filters stage locally and apply on submit (one server
  // round-trip per applied filter set, not per keystroke).
  // Free-text search (ref prefix / member number / name
  // prefix) — a SERVER query parameter, staged and applied like the
  // other drafts; nothing is filtered locally.
  const [searchDraft, setSearchDraft] = useState("");
  const [draftError, setDraftError] = useState("");

  // Date presets — CLIENT-SIDE convenience that populates date_from/date_to;
  // the server never sees a preset token. Starts on "today".
  const [datePreset, setDatePreset] = useState<"" | "today" | "7d" | "30d" | "custom">("today");
  // Anchor for the custom-range panel's outside-click dismissal. The
  // panel's visibility is its OWN state, not the preset: dismissing the
  // panel must not change which filter is selected.
  const datePanelRef = useRef<HTMLDivElement | null>(null);
  const [datePanelOpen, setDatePanelOpen] = useState(false);
  const [fromDraft, setFromDraft] = useState(todayIso);
  const [toDraft, setToDraft] = useState(todayIso);
  const [drawer, setDrawer] = useState<DrawerState>(null);

  const list = useKeysetList<Transaction>({
    queryKey: ["transactions", "list", filters, pagination.pageSize],
    fetchPage: (cursor) =>
      fetchTransactionsPage(filters, cursor, pagination.pageSize),
  });

  const mayViewMembers = can(permissions.data, "members", "view");
  // The post drawer requires the members grant to pick and FRESH-READ
  // the member before the money write (pattern (e)) — without it the
  // affordance is hidden, not disabled.
  const mayPost = can(permissions.data, "transactions", "create") && mayViewMembers;
  const mayRunInterest = can(permissions.data, "transactions", "edit");
  // The export affordance mirrors the server gate (POST /exports is
  // reports:view) — hidden, not disabled, for unentitled roles.
  const mayExport = can(permissions.data, "reports", "view");

  useEffect(() => {
    if (!datePanelOpen) return;
    function onPointerDown(event: MouseEvent) {
      const anchor = datePanelRef.current;
      if (anchor !== null && !anchor.contains(event.target as Node)) {
        // Dismissal only CLOSES the panel: whatever is staged stays
        // staged, and drafts still become server params exclusively on
        // an explicit apply.
        setDatePanelOpen(false);
      }
    }
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [datePanelOpen]);

  // The event is optional so the custom-range panel's own button can
  // drive the SAME apply path as the toolbar form's submit — one
  // validation/staging rule, never a second copy.
  function applyDrafts(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
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
      search: searchDraft.trim(),
      date_from: fromValue,
      date_to: toValue,
    }));
  }

  function applyDatePreset(preset: "" | "today" | "7d" | "30d" | "custom") {
    setDatePreset(preset);
    setDatePanelOpen(false);
    if (preset === "custom") {
      // Opening the panel pre-sets the END of the range to today, so the
      // operator only picks a start date in the common "since X" case.
      if (toDraft.trim() === "") setToDraft(presetWindow("today").to);
      setDatePanelOpen(true);
      return; // the panel's from/to inputs take over from here
    }
    if (preset === "") {
      setFromDraft("");
      setToDraft("");
      setFilters((current) => ({ ...current, date_from: "", date_to: "" }));
      return;
    }
    const { from: fromValue, to: toValue } = presetWindow(preset);
    setFromDraft(fromValue);
    setToDraft(toValue);
    setDraftError("");
    setFilters((current) => ({ ...current, date_from: fromValue, date_to: toValue }));
  }

  const columns: Column<Transaction>[] = [
    {
      key: "date",
      header: "Date",
      render: (txn) => {
        const { date, time } = fmtDateTimeParts(txn.occurred_at);
        return (
          <div className={styles.dateTime}>
            <span className={styles.date}>{date}</span>
            {time && <span className={styles.time}>{time}</span>}
          </div>
        );
      },
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
        txn.member_id === null ? (
          <span className={styles.muted}>—</span>
        ) : txn.member_no !== null ? (
          <div title={txn.member_id}>
            <div className={styles.cellStrong}>{txn.member_name}</div>
            <div className={styles.cellSub}>{txn.member_no} </div>
          </div>
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
      header: "Debit (KES)",
      align: "right",
      render: (txn) =>
        // The SERVER-reported direction places the amount; the other
        // column stays em-dash. Never netted, never summed (blocker (a)).
        txn.direction === "debit" ? (
          <span className={styles.drCell}>{fmtAmount(txn.amount)}</span>
        ) : (
          <span className={styles.emptyCell}>—</span>
        ),
    },
    {
      key: "credit",
      header: "Credit (KES)",
      align: "right",
      render: (txn) =>
        txn.direction === "credit" ? (
          <span className={styles.crCell}>{fmtAmount(txn.amount)}</span>
        ) : (
          <span className={styles.emptyCell}>—</span>
        ),
    },
  ];

  return (
    <div>
      <div className={styles.toolbarActions}>
        {mayExport && (
          <Button type="button" onClick={() => setDrawer({ mode: "export" })}>
            Export
          </Button>
        )}
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
      <Card>
        <div className={styles.toolbar}>
          <div className={styles.filters}>
            <FilterControl
              id="txn-filter-type"
              label="Type"
              value={filters.type}
              onChange={(next) =>
                setFilters((current) => ({
                  ...current,
                  type: next as TxnListFilters["type"],
                }))
              }
              options={TXN_TYPES.map((option) => ({
                value: option,
                label: TXN_TYPE_LABELS[option],
              }))}
              allLabel="All types"
            />
            <SelectFilter
              id="txn-filter-channel"
              label="Channel"
              value={filters.channel}
              onChange={(next) =>
                setFilters((current) => ({
                  ...current,
                  channel: next as TxnListFilters["channel"],
                }))
              }
              options={CHANNELS.map((option) => ({
                value: option,
                label: CHANNEL_LABELS[option],
              }))}
              allLabel="All channels"
            />

            {/* "Custom range" opens the two date fields in a panel
                anchored to this control (the select's own panel
                metaphor) instead of expanding the toolbar row. */}
            <div className={styles.datePanelAnchor} ref={datePanelRef}>
              <SelectFilter
                id="txn-filter-date-preset"
                label="Date"
                value={datePreset}
                onChange={(next) => applyDatePreset(next as "" | "today" | "7d" | "30d" | "custom")}
                options={(
                  [
                    ["today", "Today"],
                    ["7d", "Last 7 days"],
                    ["30d", "Last 30 days"],
                    ["custom", "Custom range"],
                  ] as const
                ).map(([preset, label]) => ({ value: preset, label }))}
                allLabel="All"
              />
              {datePreset === "custom" && datePanelOpen && (
                <div className={styles.datePanel} role="group" aria-label="Custom date range">
                  <div className={styles.datePanelRow}>
                    <label className={styles.datePanelLabel} htmlFor="txn-date-from">
                      From
                    </label>
                    <Input
                      id="txn-date-from"
                      type="date"
                      value={fromDraft}
                      onChange={(e) => setFromDraft(e.target.value)}
                      aria-label="Date from"
                    />
                  </div>
                  <div className={styles.datePanelRow}>
                    <label className={styles.datePanelLabel} htmlFor="txn-date-to">
                      To
                    </label>
                    <Input
                      id="txn-date-to"
                      type="date"
                      value={toDraft}
                      onChange={(e) => setToDraft(e.target.value)}
                      aria-label="Date to"
                    />
                  </div>
                  {/* Same submit path as the toolbar form: the drafts
                      only become server params on an explicit apply. */}
                  <Button type="button" onClick={() => applyDrafts()}>
                    Apply range
                  </Button>
                </div>
              )}
            </div>

            <form className={styles.searchForm} onSubmit={applyDrafts} noValidate>
              {/* Search — always visible */}
              <div className={styles.filterGroup}>
                <label className={styles.filterLabel} htmlFor="txn-filter-search">Search</label>
                <Input
                  id="txn-filter-search"
                  type="search"
                  inputMode="search"
                  className={styles.filterCompact}
                  maxLength={64}
                  placeholder="Search by ref, member no. or name…"
                  value={searchDraft}
                  onChange={(event) => setSearchDraft(event.target.value)}
                  aria-label="Search by ref, member number or name"
                />
              </div>
              {/* Staged drafts become SERVER query params only here —
                  never per keystroke. */}
              <Button type="submit">Apply</Button>
              {draftError !== "" && (
                <span className={styles.formNote} role="alert">
                  {draftError}
                </span>
              )}
            </form>
          </div>

        </div>

        <Card padded={false}>
          <KeysetTable
            columns={columns}
            query={list}
            rowKey={(txn) => txn.id}
            emptyMessage="No transactions match this filter."
            onRowClick={(txn) => setDrawer({ mode: "detail", txn })}
            pagination={{
              pageIndex: pagination.pageIndex,
              pageSize: pagination.pageSize,
              onPageChange: pagination.setPageIndex,
              onPageSizeChange: pagination.setPageSize,
              rowLabel: "transactions",
            }}
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
        {drawer !== null && drawer.mode === "export" && (
          <RequestExportDrawer
            report="transactions_ledger"
            initial={exportDraftFromFilters(filters)}
            onClose={() => setDrawer(null)}
          />
        )}
      </Card>
    </div>
  );
}
