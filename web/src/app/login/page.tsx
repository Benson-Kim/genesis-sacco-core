import { Suspense } from "react";
import type { Metadata } from "next";
import { LoginGate } from "@/modules/auth/components/LoginGate";
import { LoginGateWithNotice } from "@/modules/auth/components/LoginNotice";

export const metadata: Metadata = {
  title: "Sign in · Genesis Prestige Admin",
};

/**
 * Suspense boundary because LoginGateWithNotice reads useSearchParams
 * (the code-owned `reason` teardown flag); the fallback is the plain gate.
 */
export default function LoginPage() {
  return (
    <Suspense fallback={<LoginGate />}>
      <LoginGateWithNotice />
    </Suspense>
  );
}
