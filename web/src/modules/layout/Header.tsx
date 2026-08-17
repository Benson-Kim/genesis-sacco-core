"use client";

import { useRouter } from "next/navigation";
import { useMutation } from "@tanstack/react-query";
import { logout } from "@/modules/auth/api";
import styles from "./AppShell.module.css";

export function Header() {
  const router = useRouter();
  const signOut = useMutation({
    mutationFn: logout,
    onSettled: () => router.replace("/login"),
  });

  return (
    <header className={styles.header}>
      <div>
        <div className={styles.headerTitle}>Genesis Prestige Admin</div>
        <div className={styles.headerSub}>Genesis Prestige Sacco · FY2026</div>
      </div>
      <button
        type="button"
        className={styles.signOut}
        onClick={() => signOut.mutate()}
        disabled={signOut.isPending}
      >
        {signOut.isPending ? "Signing out…" : "Sign out"}
      </button>
    </header>
  );
}
