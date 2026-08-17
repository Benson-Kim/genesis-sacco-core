"use client";

/**
 * Settings screen (prototype tabs: Interest, Loan products,
 * Parameters, Approval matrix), wired to GET/PUT /settings and
 * the P9 products routes.
 *
 * - The settings record backs optimistic-locked edits on every tab, so
 *   its staleTime is the RECORD class (0) and panels remount keyed on
 *   the loaded version.
 * - `corrupt_keys` (stored band keys that failed read-side
 *   revalidation) surface by NAME only — the corrupt payload never
 *   reaches the client (least disclosure); names render as
 *   inert React text.
 * - Tab panels are next/dynamic chunks (Phase B speed): the products
 *   editor never loads unless its tab is opened.
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import { useQuery } from "@tanstack/react-query";
import { Banner, Card, ErrorBanner, TabList, TabPanel } from "@genesis/design-system";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { STALE_TIME } from "@/lib/query";
import { fetchSettings } from "../api";
import { SETTINGS_QUERY_KEY, SettingsViewMode } from "./SettingsSaveFlow";
import styles from "./Settings.module.css";

const InterestPanel = dynamic(
  () => import("./InterestPanel").then((m) => m.InterestPanel),
  { ssr: false, loading: () => <div className={styles.panelLoading}>Loading…</div> },
);
const ParametersPanel = dynamic(
  () => import("./ParametersPanel").then((m) => m.ParametersPanel),
  { ssr: false, loading: () => <div className={styles.panelLoading}>Loading…</div> },
);
const ApprovalPanel = dynamic(
  () => import("./ApprovalPanel").then((m) => m.ApprovalPanel),
  { ssr: false, loading: () => <div className={styles.panelLoading}>Loading…</div> },
);
const ProductsScreen = dynamic(
  () => import("@/modules/products/components/ProductsScreen").then((m) => m.ProductsScreen),
  { ssr: false, loading: () => <div className={styles.panelLoading}>Loading…</div> },
);

const TABS = [
  { id: "interest", label: "Interest" },
  { id: "products", label: "Loan products" },
  { id: "parameters", label: "Parameters" },
  { id: "approval", label: "Approval matrix" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export function useSettings() {
  return useQuery({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: fetchSettings,
    staleTime: STALE_TIME.record,
  });
}

export function SettingsScreen() {
  const [tab, setTab] = useState<TabId>("interest");
  const settings = useSettings();
  // Read-only-by-default: the Edit affordance mounts only
  // for settings:edit holders — pure UX, the server enforces the grant
  // and the optimistic version regardless (least disclosure).
  const permissions = usePermissions();
  const mayEdit = can(permissions.data, "settings", "edit");

  return (
    <div className={styles.page}>
      <TabList
        idPrefix="settings"
        tabs={TABS}
        activeId={tab}
        onChange={(next) => setTab(next as TabId)}
        ariaLabel="Settings sections"
      />

      <TabPanel idPrefix="settings" activeTabId={tab}>
      {tab === "products" ? (
        <ProductsScreen />
      ) : settings.isPending ? (
        <Card>
          <div className={styles.panelLoading}>Loading…</div>
        </Card>
      ) : settings.isError || settings.data === undefined ? (
        <Card>
          <ErrorBanner error={settings.error} />
        </Card>
      ) : (
        <>
          {!settings.data.configured && (
            <Banner>
              Settings are not configured yet — consumers run on documented
              fallback defaults. Saving a tab claims the first settings
              record.
            </Banner>
          )}
          {settings.data.corrupt_keys.length > 0 && (
            <Banner variant="error">
              These stored settings failed integrity revalidation and read as
              not configured: {settings.data.corrupt_keys.join(", ")}.
              Re-save them to repair the stored value.
            </Banner>
          )}
          {/* Read-only-by-default: ONE shared shell per
              tab (reuse-first); the version key remounts the shell after
              every save, landing back in read-only view. */}
          {tab === "interest" && (
            <SettingsViewMode
              key={`interest:${settings.data.version}`}
              mayEdit={mayEdit}
              panelName="interest rules"
            >
              {(editing) => <InterestPanel settings={settings.data!} editing={editing} />}
            </SettingsViewMode>
          )}
          {tab === "parameters" && (
            <SettingsViewMode
              key={`parameters:${settings.data.version}`}
              mayEdit={mayEdit}
              panelName="parameters"
            >
              {(editing) => <ParametersPanel settings={settings.data!} editing={editing} />}
            </SettingsViewMode>
          )}
          {tab === "approval" && (
            <SettingsViewMode
              key={`approval:${settings.data.version}`}
              mayEdit={mayEdit}
              panelName="approval matrix"
            >
              {(editing) => <ApprovalPanel settings={settings.data!} editing={editing} />}
            </SettingsViewMode>
          )}
        </>
      )}
      </TabPanel>
    </div>
  );
}
