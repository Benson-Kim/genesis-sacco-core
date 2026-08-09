"use client";

/**
 * Shared save flow for the tenant-settings panels (one copy,
 * reuse-first): every settings tab commits through this hook + UI pair.
 *
 * - Settings ARE the money parameters, so EVERY save is money-adjacent
 *   and flows through ConfirmDangerModal's typed confirmation (Phase B
 *   blocker (f)).
 * - One Idempotency-Key slot per panel: identical retries reuse the
 *   key, changed content rotates it (concurrency safety); mutations run with the
 *   global retry: 0 — exactly one write attempt per confirmation.
 * - Optimistic-lock 409 renders the shared ConflictBanner's explicit
 *   reload-and-re-enter flow (panels remount keyed on the fresh
 *   version; the stale submission is never replayed). Non-409 failures
 *   render the least-disclosure ErrorBanner.
 * - Async outcomes are announced via the shared LiveAnnouncer with
 *   operator-facing copy only.
 */
import { useRef, useState, type ReactNode } from "react";
import { useMutation, useQueryClient, type UseMutationResult } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal } from "@genesis/design-system";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { isConflict } from "@/lib/errors";
import { updateSettings, type UpdateSettingsInput } from "../api";
import type { Settings } from "../schemas";
import styles from "./Settings.module.css";

export const SETTINGS_QUERY_KEY = ["settings", "record"] as const;

/**
 * The keys a pending WYSIWYG body would CLEAR (W57-3): submitted as an
 * explicit null while the loaded record still holds a value. Null-ness
 * comparison ONLY — no money value is ever compared or computed. The
 * names surface inside the typed confirmation so the operator's typed
 * phrase covers explicit knowledge of every clear.
 */
export function clearedKeyNames(input: UpdateSettingsInput, current: Settings): string[] {
  const cleared: string[] = [];
  for (const [key, value] of Object.entries(input)) {
    if (key === "version" || value !== null) continue;
    if (current[key as keyof Settings] !== null) cleared.push(key);
  }
  return cleared;
}

export interface SettingsSaveFlow {
  save: UseMutationResult<Settings, Error, UpdateSettingsInput>;
  /** The validated body awaiting typed confirmation (null = none). */
  pendingInput: UpdateSettingsInput | null;
  /** Stage a validated body for confirmation. */
  requestSave: (input: UpdateSettingsInput) => void;
  /** Dismiss the confirmation without writing. */
  cancel: () => void;
}

export function useSettingsSaveFlow(panel: string): SettingsSaveFlow {
  const queryClient = useQueryClient();
  const [pendingInput, setPendingInput] = useState<UpdateSettingsInput | null>(null);
  const slot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const save = useMutation({
    mutationFn: (input: UpdateSettingsInput) =>
      updateSettings(
        input,
        idempotencyKeyFor(slot.current, JSON.stringify({ op: "settings-update", panel, input })),
      ),
    onSuccess: (fresh) => {
      setPendingInput(null);
      queryClient.setQueryData(SETTINGS_QUERY_KEY, fresh);
      announce("Settings saved.");
    },
    // Close the confirmation on failure so the banner is what the
    // operator sees; re-entering is a NEW confirmed intent — never an
    // auto-retry.
    onError: () => {
      setPendingInput(null);
      announce("Settings save failed.", "assertive");
    },
  });

  return {
    save,
    pendingInput,
    requestSave: (input) => setPendingInput(input),
    cancel: () => {
      if (!save.isPending) setPendingInput(null);
    },
  };
}

/**
 * Read-only-by-default shell for the settings panels (
 * ONE copy: every panel that consumes SettingsSaveFlow is
 * wrapped by this shell in SettingsScreen).
 *
 * - VIEW MODE is the default: every control inside is disabled
 *   STRUCTURALLY by a disabled <fieldset> — no field edit, no submit,
 *   no staged confirmation can exist before the operator explicitly
 *   enters edit mode (the edit-by-mistake class is closed by
 *   construction, not by convention).
 * - The Edit affordance is PERMISSION-GATED (pure UX — the server
 *   enforces settings:edit and the optimistic version regardless,
 *   least disclosure).
 * - Cancel discards by REMOUNT (the epoch key): the panel
 *   re-initializes from the loaded server record — server truth
 *   restored, no draft survives.
 * - A successful save bumps the record version, which remounts this
 *   shell via SettingsScreen's version key — the tab lands back in
 *   read-only view showing the fresh server record.
 */
export function SettingsViewMode({
  mayEdit,
  panelName,
  children,
}: Readonly<{
  mayEdit: boolean;
  /** Operator-facing name in the Edit/Cancel affordances. */
  panelName: string;
  /** Render prop receiving the live edit-mode flag. */
  children: (editing: boolean) => ReactNode;
}>) {
  const [editing, setEditing] = useState(false);
  const [epoch, setEpoch] = useState(0);
  return (
    <div>
      <div className={styles.viewModeBar}>
        <span className={styles.viewModeNote}>
          {editing
            ? `Editing ${panelName} — Cancel discards unsaved changes.`
            : "Read-only view — values shown are the saved server record."}
        </span>
        {mayEdit && !editing && (
          <Button type="button" onClick={() => setEditing(true)}>
            Edit {panelName}
          </Button>
        )}
        {editing && (
          <Button
            type="button"
            onClick={() => {
              // Discard-by-remount: the epoch key throws every draft
              // away; the panel re-reads the loaded server record.
              setEditing(false);
              setEpoch((current) => current + 1);
            }}
          >
            Cancel — discard changes
          </Button>
        )}
      </div>
      <fieldset key={epoch} disabled={!editing} className={styles.viewModeFieldset}>
        {children(editing)}
      </fieldset>
    </div>
  );
}

/**
 * Banners + submit button + typed-confirmation modal. Render INSIDE the
 * panel's <form>; the panel validates on submit and calls requestSave
 * with the parsed body.
 */
export function SettingsSaveControls({
  flow,
  mayEdit,
  buttonLabel,
  confirmTitle,
  confirmPhrase,
  settings,
  children,
}: Readonly<{
  flow: SettingsSaveFlow;
  mayEdit: boolean;
  buttonLabel: string;
  confirmTitle: string;
  /** The exact phrase the operator must type (tab-specific). */
  confirmPhrase: string;
  /** The loaded record backing this tab — used to name the keys a
   * pending body would CLEAR (W57-3). */
  settings: Settings;
  /** Modal summary copy (operator-facing only). */
  children?: ReactNode;
}>) {
  const queryClient = useQueryClient();
  const conflict = flow.save.isError && isConflict(flow.save.error);
  const clearedKeys =
    flow.pendingInput === null ? [] : clearedKeyNames(flow.pendingInput, settings);

  return (
    <>
      {/* Explicit reload flow (one copy — reuse-first): refetch the record;
          the panel remounts keyed on the fresh version. The stale
          submission is NEVER replayed. */}
      <ConflictBanner
        error={flow.save.error}
        onReload={() => {
          void queryClient.refetchQueries({ queryKey: SETTINGS_QUERY_KEY });
        }}
      />
      {flow.save.isError && !conflict && <ErrorBanner error={flow.save.error} />}
      {mayEdit && (
        <div className={styles.saveRow}>
          <Button variant="primary" type="submit" disabled={flow.save.isPending}>
            {flow.save.isPending ? "Saving…" : buttonLabel}
          </Button>
        </div>
      )}
      {flow.pendingInput !== null && (
        <ConfirmDangerModal
          title={confirmTitle}
          confirmPhrase={confirmPhrase}
          confirmLabel={confirmTitle}
          pending={flow.save.isPending}
          onClose={flow.cancel}
          onConfirm={() => {
            if (!flow.save.isPending && flow.pendingInput !== null) {
              flow.save.mutate(flow.pendingInput);
            }
          }}
        >
          {/* Explicit-clear disclosure (W57-3): the typed confirmation
              names every stored key this save would CLEAR, so a blank
              field never clears config without the operator knowing. */}
          {clearedKeys.length > 0 && (
            <Banner variant="error">
              This save CLEARS the stored value of: {clearedKeys.join(", ")}.
            </Banner>
          )}
          {children}
        </ConfirmDangerModal>
      )}
    </>
  );
}
