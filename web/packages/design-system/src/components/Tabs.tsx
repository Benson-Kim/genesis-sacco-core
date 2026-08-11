"use client";

import { useRef, type KeyboardEvent, type ReactNode } from "react";
import styles from "./Tabs.module.css";

export interface TabDef {
    id: string;
    label: string;
}

export interface TabListProps {
    /** Namespaces the generated tab/panel ids — unique per screen instance
     * (e.g. "settings", "guarantors") so multiple tab groups on one page
     * never collide. */
    idPrefix: string;
    tabs: readonly TabDef[];
    activeId: string;
    onChange: (id: string) => void;
    ariaLabel: string;
}

/**
 * Conforming ARIA tabs row (roving tabindex, automatic activation):
 * one tab stop for the whole tablist; Left/Right (wrapping) and
 * Home/End move BOTH selection and focus. Promoted from the
 * settings-screen module-local copy (reuse-first) — pair with
 * `TabPanel` (same `idPrefix`) for the aria-controls/aria-labelledby
 * association.
 */
export function TabList({ idPrefix, tabs, activeId, onChange, ariaLabel }: Readonly<TabListProps>) {
    const tabRefs = useRef<Map<string, HTMLButtonElement>>(new Map());

    function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
        const order = tabs.map((entry) => entry.id);
        const index = order.indexOf(activeId);
        let next: string | undefined;
        if (event.key === "ArrowRight") next = order[(index + 1) % order.length];
        else if (event.key === "ArrowLeft") next = order[(index - 1 + order.length) % order.length];
        else if (event.key === "Home") next = order[0];
        else if (event.key === "End") next = order[order.length - 1];
        if (next !== undefined) {
            event.preventDefault();
            onChange(next);
            tabRefs.current.get(next)?.focus();
        }
    }

    return (
        <div className={styles.tabs} role="tablist" aria-label={ariaLabel} onKeyDown={onKeyDown}>
            {tabs.map((entry) => (
                <button
                    key={entry.id}
                    ref={(node) => {
                        if (node !== null) tabRefs.current.set(entry.id, node);
                        else tabRefs.current.delete(entry.id);
                    }}
                    type="button"
                    role="tab"
                    id={`${idPrefix}-tab-${entry.id}`}
                    aria-selected={entry.id === activeId}
                    aria-controls={`${idPrefix}-tabpanel`}
                    tabIndex={entry.id === activeId ? 0 : -1}
                    className={entry.id === activeId ? `${styles.tab} ${styles.tabActive}` : styles.tab}
                    onClick={() => onChange(entry.id)}
                >
                    {entry.label}
                </button>
            ))}
        </div>
    );
}

export interface TabPanelProps {
    idPrefix: string;
    /** The currently active tab id — drives aria-labelledby; callers
     * conditionally render only the active tab's content as children
     * (the settings-screen convention), so an inactive tab's data/query
     * never mounts. */
    activeTabId: string;
    children: ReactNode;
}

export function TabPanel({ idPrefix, activeTabId, children }: Readonly<TabPanelProps>) {
    return (
        <div role="tabpanel" id={`${idPrefix}-tabpanel`} aria-labelledby={`${idPrefix}-tab-${activeTabId}`}>
            {children}
        </div>
    );
}
