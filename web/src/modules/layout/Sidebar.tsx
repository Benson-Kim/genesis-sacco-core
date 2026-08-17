"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { NAV_SECTIONS } from "./nav";
import styles from "./AppShell.module.css";

/**
 * Prototype sidebar. Items gated by /me/permissions are hidden for roles
 * without view rights — the API still enforces on every call (gate 1.6).
 */
export function Sidebar() {
  const pathname = usePathname();
  const permissions = usePermissions();

  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}>
        <div className={styles.mark}>G</div>
        <div>
          <div className={styles.brandName}>Genesis Prestige</div>
          <div className={styles.brandTag}>ZURI GENESIS · SACCO</div>
        </div>
      </div>
      <nav className={styles.nav}>
        {NAV_SECTIONS.map((section) => {
          const visible = section.items.filter(
            (item) => item.module === null || can(permissions.data, item.module, "view"),
          );
          if (visible.length === 0) return null;
          return (
            <div key={section.label}>
              <div className={styles.navSection}>{section.label}</div>
              {visible.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className={
                    pathname === item.href
                      ? `${styles.navItem} ${styles.navItemOn}`
                      : styles.navItem
                  }
                >
                  {item.label}
                </Link>
              ))}
            </div>
          );
        })}
      </nav>
    </aside>
  );
}
