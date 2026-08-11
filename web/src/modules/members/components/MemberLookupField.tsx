"use client";

/**
 * Shared member picker: resolve ONE member by a unique identifier —
 * member number or national ID — instead of paging the whole
 * membership into a <select>.
 *
 * Why this shape (the design decision this component exists to enforce):
 * - A full-membership dropdown is O(tenant size) on every open, leaks
 *   the entire member roster to anyone who can open the drawer, and
 *   gets unusable past a few hundred members. This field sends ONE
 *   exact-match read for the identifier the operator actually typed.
 * - The lookup fires on BLUR or on the search button that sits on the
 *   same row as the input — never per keystroke, so typing a member
 *   number costs exactly one request.
 * - The server answers with the member's NAME plus the OPPOSITE
 *   identifier (search by number → name + masked ID; search by ID →
 *   name + number), so the operator can confirm they resolved the
 *   right person without ever seeing the full credential. The ID is
 *   masked SERVER-side (application/members.py) — the full value never
 *   reaches this client.
 * - Editing the input WITHDRAWS the prior resolution: a caller can
 *   only ever act on a member the operator actually resolved, never on
 *   a stale one left behind by an edit (the PostTransactionDrawer /
 *   RequestTransferDrawer precedent this generalises).
 */
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, FormField, Input, Select, ErrorBanner } from "@genesis/design-system";
import { STALE_TIME } from "@/lib/query";
import { lookupMemberByIdNumber, lookupMemberByNo } from "../api";
import type { Member } from "../schemas";
import styles from "./Members.module.css";

export type MemberIdKind = "member_no" | "id_number";

export interface MemberLookupFieldProps {
  /** Unique id prefix — one field can mount twice on a screen (a
   * two-leg transfer), so every control id derives from this. */
  idPrefix: string;
  /** Optional heading above the control (e.g. "Transferring member"). */
  legLabel?: string;
  hint?: string;
  disabled?: boolean;
  /** Called with the resolved member, or null whenever the resolution
   * is withdrawn (edit, kind switch, a miss, or a rejection). */
  onResolved: (member: Member | null) => void;
  /** Caller-side constraint on WHO may be selected (e.g. a borrower
   * may not guarantee their own loan). Return a refusal message to
   * reject the resolved member, or null to accept. The field owns the
   * display: a rejected member is reported to the parent as null and
   * shows the refusal INSTEAD of the found-confirmation, so the screen
   * can never state both at once. */
  reject?: (member: Member) => string | null;
}

export function MemberLookupField({
  idPrefix,
  legLabel,
  hint,
  disabled = false,
  onResolved,
  reject,
}: Readonly<MemberLookupFieldProps>) {
  const [idKind, setIdKind] = useState<MemberIdKind>("member_no");
  const [input, setInput] = useState("");
  const [lookupValue, setLookupValue] = useState("");

  const lookup = useQuery({
    queryKey: ["members", "lookup", idKind, lookupValue === "" ? "none" : lookupValue],
    queryFn: () =>
      idKind === "member_no" ? lookupMemberByNo(lookupValue) : lookupMemberByIdNumber(lookupValue),
    enabled: lookupValue !== "",
    staleTime: STALE_TIME.record,
  });

  const found = lookup.data ?? null;
  // A rejected member is NOT a selection: the parent is told null, and
  // the refusal replaces the confirmation below.
  const refusal = found !== null && reject !== undefined ? reject(found) : null;
  const resolved = refusal === null ? found : null;

  // The callback is held in a ref and deliberately kept OUT of the
  // effect's dependencies. Callers legitimately pass an inline arrow
  // (e.g. `member => onChange(member?.id ?? "")`), whose identity
  // changes every render; depending on it would re-fire the effect,
  // setState in the parent, re-render, and loop forever. Only a real
  // change in the RESOLUTION should notify the parent.
  const onResolvedRef = useRef(onResolved);
  useEffect(() => {
    onResolvedRef.current = onResolved;
  });
  useEffect(() => {
    // Report as an EFFECT, never during render — a parent setState call
    // belongs outside the render phase.
    if (lookup.isSuccess) onResolvedRef.current(resolved);
  }, [lookup.isSuccess, resolved]);

  /** Withdraw any prior resolution: the caller can only ever act on
   * what the operator actually resolved. */
  function withdraw() {
    setLookupValue("");
    onResolvedRef.current(null);
  }

  const label = idKind === "member_no" ? "Member number" : "National ID number";

  return (
    <div>
      {legLabel !== undefined && <div className={styles.subhead}>{legLabel}</div>}
      <FormField id={`${idPrefix}-kind`} label="Look up by">
        {(control) => (
          <Select
            {...control}
            value={idKind}
            onChange={(event) => {
              setIdKind(event.target.value as MemberIdKind);
              // Switching identifier kinds withdraws the resolution AND
              // the typed value: the write can only target what the
              // operator re-enters under the new kind.
              setInput("");
              withdraw();
            }}
            disabled={disabled}
          >
            <option value="member_no">Member number</option>
            <option value="id_number">National ID number</option>
          </Select>
        )}
      </FormField>
      <div className={styles.lookupRow}>
        <FormField id={`${idPrefix}-input`} label={label} hint={hint}>
          {(control) => (
            <Input
              {...control}
              inputMode="search"
              maxLength={32}
              value={input}
              onChange={(event) => {
                setInput(event.target.value);
                withdraw();
              }}
              onBlur={() => setLookupValue(input.trim())}
              disabled={disabled}
            />
          )}
        </FormField>
        {/* Same-row search affordance: blur is not discoverable on its
            own, and keyboard users tabbing away should not be the only
            way to trigger a lookup. */}
        <Button
          type="button"
          onClick={() => setLookupValue(input.trim())}
          disabled={disabled || input.trim() === ""}
          aria-label={`Search by ${label.toLowerCase()}`}
        >
          Search
        </Button>
      </div>

      {lookupValue !== "" && lookup.isPending && (
        <div className={styles.formNote}>Looking up {lookupValue}…</div>
      )}
      {lookupValue !== "" && lookup.isError && <ErrorBanner error={lookup.error} />}
      {lookupValue !== "" && !lookup.isPending && !lookup.isError && found === null && (
        <div className={styles.formNote} role="status">
          {idKind === "member_no"
            ? `No member found with number ${lookupValue} — check the number and try again.`
            : "No member found with that ID number — check it and try again."}
        </div>
      )}
      {refusal !== null && (
        <div className={styles.formNote} role="alert">
          {refusal}
        </div>
      )}
      {resolved !== null && (
        // Confirmation carries the OPPOSITE identifier from the one
        // searched, so the operator can verify the match without the
        // system ever echoing back what they just typed. Worded
        // "found", NOT "verified": drawers that follow this with a
        // binding fresh read say "Member verified: … (record vN)", and
        // two identically-prefixed notes would be redundant and
        // ambiguous about which one is the binding check.
        <div className={styles.formNote} role="status">
          Member found: {resolved.name}
          {idKind === "member_no"
            ? resolved.id_number_masked !== null && ` · ID ${resolved.id_number_masked}`
            : ` · ${resolved.member_no}`}
          {resolved.status !== "active" ? ` — status: ${resolved.status}` : ""}
        </div>
      )}
    </div>
  );
}
