"use client";

/**
 * Audit-log viewer (the governance counterpart of the audit-write data integrity), salvaged from duo/feature/p13-5-frontend-followthrough
 * @ 198a238 (itself ported from onto the scaffold).
 *
 * Security posture (carried forward):
 * - before/after payloads are ATTACKER-INFLUENCED data (they embed member
 *   names, branches, purposes…). They render as pretty-printed JSON TEXT
 *   through React interpolation into a <pre> — exactly as the API
 *   returned them. No client-side reconstruction of redacted fields, no
 *   interpretation, no HTML parsing (the named XSS threat).
 * - Keyset pagination only: opaque cursors, echoed back, never parsed or
 *   rendered.
 * - Filters are length/shape-limited client-side (UUID check for the
 *   actor filter avoids 422 noise); the server validates and enforces
 *   entitlement regardless.
 */
import { useState, type FormEvent } from "react";
import { Banner, Button, Card, Kv, Modal, Pill } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { fmtDateTime, isUuid, prettyJson } from "@/lib/format";
import { fetchAuditPage } from "../api";
import { EMPTY_AUDIT_FILTERS, type AuditEntry, type AuditFilters } from "../schemas";
import { FormField } from "@/modules/forms/FormField";
import styles from "./Audit.module.css";

export function AuditScreen() {
  const [draft, setDraft] = useState<AuditFilters>(EMPTY_AUDIT_FILTERS);
  const [filters, setFilters] = useState<AuditFilters>(EMPTY_AUDIT_FILTERS);
  const [filterError, setFilterError] = useState("");
  const [selected, setSelected] = useState<AuditEntry | null>(null);

  const list = useKeysetList<AuditEntry>({
    queryKey: ["audit", "list", filters],
    fetchPage: (cursor) => fetchAuditPage(filters, cursor),
  });

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const actor = draft.actorId.trim();
    if (actor !== "" && !isUuid(actor)) {
      setFilterError("Actor filter must be a UUID.");
      return;
    }
    setFilterError("");
    setFilters({
      entity: draft.entity.trim(),
      action: draft.action.trim(),
      actorId: actor,
      dateFrom: draft.dateFrom,
      dateTo: draft.dateTo,
    });
  }

  function clearFilters() {
    setFilterError("");
    setDraft(EMPTY_AUDIT_FILTERS);
    setFilters(EMPTY_AUDIT_FILTERS);
  }

  const columns: Column<AuditEntry>[] = [
    {
      key: "at",
      header: "At",
      render: (entry) => (
        <span className={`${styles.muted} ${styles.tnum}`}>{fmtDateTime(entry.at)}</span>
      ),
    },
    {
      key: "actor",
      header: "Actor",
      render: (entry) => (
        <span className={styles.cellSub}>{entry.actor_id ?? "system"}</span>
      ),
    },
    {
      key: "action",
      header: "Action",
      render: (entry) => <Pill>{entry.action}</Pill>,
    },
    {
      key: "entity",
      header: "Entity",
      render: (entry) => <span className={styles.cellStrong}>{entry.entity}</span>,
    },
    {
      key: "entity_id",
      header: "Entity id",
      render: (entry) => <span className={styles.cellSub}>{entry.entity_id}</span>,
    },
    {
      key: "payload",
      header: "Payload",
      align: "right",
      render: (entry) =>
        entry.redacted ? (
          <Pill>Redacted</Pill>
        ) : (
          <Pill bg="emeraldSoft" color="emerald">
            View ›
          </Pill>
        ),
    },
  ];

  return (
    <Card padded={false}>
      <form className={styles.toolbar} onSubmit={applyFilters}>
        <div className={styles.filters}>
          <FormField id="audit-filter-entity" label="Entity">
            {(control) => (
              <input
                {...control}
                className={styles.input}
                maxLength={100}
                placeholder="e.g. users"
                value={draft.entity}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, entity: event.target.value }))
                }
              />
            )}
          </FormField>
          <FormField id="audit-filter-action" label="Action">
            {(control) => (
              <input
                {...control}
                className={styles.input}
                maxLength={100}
                placeholder="e.g. user.role"
                value={draft.action}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, action: event.target.value }))
                }
              />
            )}
          </FormField>
          <FormField id="audit-filter-actor" label="Actor">
            {(control) => (
              <input
                {...control}
                className={styles.input}
                maxLength={36}
                placeholder="actor UUID"
                value={draft.actorId}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, actorId: event.target.value }))
                }
              />
            )}
          </FormField>
          <FormField id="audit-filter-from" label="From">
            {(control) => (
              <input
                {...control}
                className={styles.input}
                type="date"
                value={draft.dateFrom}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, dateFrom: event.target.value }))
                }
              />
            )}
          </FormField>
          <FormField id="audit-filter-to" label="To">
            {(control) => (
              <input
                {...control}
                className={styles.input}
                type="date"
                value={draft.dateTo}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, dateTo: event.target.value }))
                }
              />
            )}
          </FormField>
        </div>
        <div className={styles.actions}>
          <Button type="button" onClick={clearFilters}>
            Clear
          </Button>
          <Button variant="primary" type="submit">
            Apply filters
          </Button>
        </div>
      </form>
      {filterError !== "" && (
        <div className={styles.pad}>
          <Banner variant="error">{filterError}</Banner>
        </div>
      )}
      <KeysetTable
        columns={columns}
        query={list}
        rowKey={(entry) => String(entry.id)}
        emptyMessage="No audit entries match the current filters."
        onRowClick={(entry) => setSelected(entry)}
      />
      {selected !== null && (
        <AuditEntryDrawer entry={selected} onClose={() => setSelected(null)} />
      )}
    </Card>
  );
}

function PayloadBlock({ label, value, redacted }: { label: string; value: unknown; redacted: boolean }) {
  return (
    <div>
      <div className={styles.payloadLabel}>{label}</div>
      {value === null || value === undefined ? (
        <div className={styles.cellSub}>
          {redacted ? "Redacted per role entitlement." : "—"}
        </div>
      ) : (
        <pre className={styles.payload}>{prettyJson(value)}</pre>
      )}
    </div>
  );
}

function AuditEntryDrawer({ entry, onClose }: { entry: AuditEntry; onClose: () => void }) {
  return (
    <Modal title="Audit entry" onClose={onClose}>
      <div className={styles.detailGrid}>
        {/* Design-system Kv rows — quiet
            variant, byte-equivalent rules to the module CSS this
            replaced; the At row's composed tnum class passes through
            valueClassName, keeping the rendered class set equivalent. */}
        <Kv label="At" variant="quiet" valueClassName={styles.tnum}>
          {fmtDateTime(entry.at)}
        </Kv>
        <Kv label="Actor" variant="quiet">{entry.actor_id ?? "system"}</Kv>
        <Kv label="Action" variant="quiet">{entry.action}</Kv>
        <Kv label="Entity" variant="quiet">{entry.entity}</Kv>
        <Kv label="Entity id" variant="quiet">{entry.entity_id}</Kv>
        <Kv label="Payload access" variant="quiet">
          {entry.redacted ? "Redacted" : "Disclosed per role"}
        </Kv>
      </div>
      {entry.redacted && (
        <Banner>
          Payload redacted: your role cannot view the module owning this entity.
          Row metadata is the audit trail itself.
        </Banner>
      )}
      <PayloadBlock label="Before" value={entry.before} redacted={entry.redacted} />
      <PayloadBlock label="After" value={entry.after} redacted={entry.redacted} />
    </Modal>
  );
}
