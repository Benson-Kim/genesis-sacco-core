"use client";

/**
 * FilterControl — reusable declared-filter primitive (design-system).
 *
 * Rendering rule:
 *   options.length <= 4  →  segmented button group (segment variant)
 *   options.length >= 5  →  native <select> with CSS arrow (select variant)
 *
 * Count display:
 *   Each option may carry an optional `count`. When present it renders as a
 *   small badge after the label so the user can see how many items match
 *   before applying the filter. A null/undefined count is silently omitted —
 *   the filter still works without counts.
 *
 * Values:
 *   All values are code-owned vocabulary supplied by the caller. The empty
 *   string "" means "no filter applied" (the caller should send no query
 *   parameter for this state). The component never touches URLs or query
 *   state directly.
 *
 * Accessibility:
 *   - Segment variant: role="group" + aria-label on the container;
 *     aria-pressed on each button (ARIA button pattern).
 *   - Select variant: <label htmlFor> wired to the <select> id.
 *   - Count badge: aria-hidden so screen readers read "Rejected (12)" as
 *     announced by the option text, not as a separate element.
 */
import styles from "./FilterControl.module.css";

// ─── Public types ────────────────────────────────────────────────────────────

export interface FilterOption<V extends string> {
  /** The filter value sent to the API. Use "" only for the "all" sentinel. */
  value: V;
  /** Human-readable label rendered in the control. */
  label: string;
  /**
   * Advisory count rendered as a badge. Omit or pass null when counts are
   * unavailable (e.g. still loading) — the filter works either way.
   */
  count?: number | null;
  /**
   * When true, the badge renders as "n+" to indicate the real count exceeds
   * what was fetched (e.g. a paged fetch hit its limit). Ignored when
   * `count` is null/undefined.
   */
  countMore?: boolean;
}

export interface FilterControlProps<V extends string> {
  /** Accessible label. Rendered as the visible persistent label above the
   *  control (never a placeholder). */
  label: string;
  /**
   * Stable HTML id — forwarded to the <select> id (select variant) or the
   * wrapping group container id (segment variant) for aria wiring at the
   * call site.
   */
  id: string;
  /** Currently active filter value. "" = no filter / show all. */
  value: V | "";
  /**
   * Declared options. The length drives which variant is rendered:
   *   ≤ 4  →  segment buttons
   *   ≥ 5  →  select dropdown
   */
  options: ReadonlyArray<FilterOption<V>>;
  onChange: (next: V | "") => void;
  /** Label text for the "show all" option (default: "All"). */
  allLabel?: string;
  /**
   * Advisory total count shown on the "All" button/option — typically the
   * sum of all per-option counts. Omit or pass null while loading.
   */
  allCount?: number | null;
  /** When true the "All" badge renders as "n+". */
  allCountMore?: boolean;
  /**
   * When false the "show all" sentinel is not rendered at all — for
   * vocabularies with NO "all" state (the caller's value is always a
   * concrete option and "" is never emitted). Default: true.
   */
  allOption?: boolean;
}

// ─── Internal helpers ────────────────────────────────────────────────────────

/** Small count badge appended inside a segment button or select option. */
function CountBadge({
  count,
  countMore,
}: {
  count: number | null | undefined;
  countMore?: boolean;
}) {
  if (count === null || count === undefined) return null;
  return (
    <span className={styles.count} aria-hidden="true">
      {count}{countMore ? "+" : ""}
    </span>
  );
}

/** Text representation of a count for <option> elements (no JSX). */
function countText(count: number | null | undefined, countMore?: boolean): string {
  if (count === null || count === undefined) return "";
  return ` (${count}${countMore ? "+" : ""})`;
}

// ─── Segment variant (≤ 4 options) ──────────────────────────────────────────

function SegmentFilter<V extends string>({
  label,
  id,
  value,
  options,
  onChange,
  allLabel = "All",
  allCount,
  allCountMore,
  allOption = true,
}: FilterControlProps<V>) {
  return (
    <div className={styles.filterGroup}>
      {/* Label is visually hidden but kept for aria-labelledby — screen
          readers announce the group name without cluttering the toolbar. */}
      <span className={styles.filterLabel} id={`${id}-label`} aria-hidden="false">
        {label}
      </span>
      <div
        className={styles.segment}
        role="group"
        aria-labelledby={`${id}-label`}
        id={id}
      >
        {allOption && (
          <button
            type="button"
            className={styles.segmentButton}
            aria-pressed={value === ""}
            onClick={() => onChange("")}
          >
            {allLabel}
            <CountBadge count={allCount} countMore={allCountMore} />
          </button>
        )}
        {options.map((opt) => (
          <button
            key={opt.value}
            type="button"
            className={styles.segmentButton}
            aria-pressed={value === opt.value}
            onClick={() => onChange(opt.value)}
          >
            {opt.label}
            <CountBadge count={opt.count} countMore={opt.countMore} />
          </button>
        ))}
      </div>
    </div>
  );
}

// ─── Select variant (≥ 5 options) ───────────────────────────────────────────

function SelectFilter<V extends string>({
  label,
  id,
  value,
  options,
  onChange,
  allLabel = "All",
  allCount,
  allCountMore,
  allOption = true,
}: FilterControlProps<V>) {
  return (
    <div className={styles.filterGroup}>
      {/* Visually hidden label — wired to the select for a11y. */}
      <label className={styles.filterLabel} htmlFor={id}>
        {label}
      </label>
      <div className={styles.selectWrap}>
        <select
          id={id}
          className={styles.filterSelect}
          value={value}
          onChange={(e) => onChange(e.target.value as V | "")}
        >
          {allOption && (
            <option value="">{allLabel}{countText(allCount, allCountMore)}</option>
          )}
          {options.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}{countText(opt.count, opt.countMore)}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

// ─── Public component ────────────────────────────────────────────────────────

/**
 * FilterControl — auto-selects segment vs select rendering.
 *
 * ```tsx
 * <FilterControl
 *   id="app-stage"
 *   label="Stage"
 *   value={stage}
 *   onChange={setStage}
 *   options={APPLICATION_STAGES.map((s) => ({
 *     value: s,
 *     label: STAGE_LABELS[s],
 *     count: counts[s],
 *   }))}
 * />
 * ```
 */
export function FilterControl<V extends string>(
  props: Readonly<FilterControlProps<V>>,
) {
  if (props.options.length >= 5) {
    return <SelectFilter {...props} />;
  }
  return <SegmentFilter {...props} />;
}
