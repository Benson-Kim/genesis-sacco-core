"use client";

import { useSyncExternalStore } from "react";
import { hasSession, subscribe } from "./session";

/** Reactive "is there a session in this tab?" flag (false during SSR). */
export function useSession(): boolean {
  return useSyncExternalStore(subscribe, hasSession, () => false);
}
