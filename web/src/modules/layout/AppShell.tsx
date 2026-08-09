"use client";

import type { ReactNode } from "react";
import { Sidebar } from "./Sidebar";
import { Header } from "./Header";
import { LiveAnnouncer } from "./announcer";
import { MAIN_CONTENT_ID, SkipToContent } from "./SkipToContent";
import styles from "./AppShell.module.css";

/**
 * Prototype app frame: navy sidebar, top header, scrollable main area.
 * Accessibility shell primitives: skip-to-content as the first
 * focusable element and the single shared aria-live region.
 */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className={styles.app}>
      <SkipToContent />
      <Sidebar />
      <div className={styles.main}>
        <Header />
        <main className={styles.content} id={MAIN_CONTENT_ID} tabIndex={-1}>
          {children}
        </main>
      </div>
      <LiveAnnouncer />
    </div>
  );
}
