import type { Metadata } from "next";
import { Suspense } from "react";
import { LoginGate } from "@/modules/auth/components/LoginGate";
import { LoginGateWithNotice } from "@/modules/auth/components/LoginNotice";

export const metadata: Metadata = {
  title: "Sign in · Genesis Prestige Admin",
};

export default function LoginPage() {
  // Suspense boundary required by useSearchParams during prerender; the
  // fallback is the same gate without the notice.
  return (
    <Suspense fallback={<LoginGate />}>
      <LoginGateWithNotice />
    </Suspense>
  );
}
