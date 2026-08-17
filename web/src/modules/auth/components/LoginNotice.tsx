"use client";

import { useSearchParams } from "next/navigation";
import { LoginGate } from "./LoginGate";

/**
 * Maps the `reason` query flag (a code-owned enum value, never free text —
 * nothing attacker-controlled is echoed) to the operator notice shown on
 * the gate, e.g. after a 401 teardown.
 */
const NOTICES: Record<string, string> = {
  expired: "Session expired — sign in again.",
  "signed-out": "Signed out.",
};

export function LoginGateWithNotice() {
  const params = useSearchParams();
  const reason = params.get("reason");
  const notice = reason !== null ? NOTICES[reason] : undefined;
  return <LoginGate notice={notice} />;
}
