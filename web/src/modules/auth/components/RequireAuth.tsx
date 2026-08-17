"use client";

import { useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { useSession } from "../useSession";
import styles from "./RequireAuth.module.css";

/**
 * Client-side gate: redirects to /login when no session exists. This only
 * shapes UX — the API enforces authn/authz on every call (gate 1.6).
 */
export function RequireAuth({ children }: Readonly<{ children: ReactNode }>) {
  const authed = useSession();
  const router = useRouter();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (mounted && !authed) {
      router.replace("/login");
    }
  }, [mounted, authed, router]);

  if (!mounted || !authed) {
    return <div className={styles.checking}>Checking session…</div>;
  }
  return <>{children}</>;
}
