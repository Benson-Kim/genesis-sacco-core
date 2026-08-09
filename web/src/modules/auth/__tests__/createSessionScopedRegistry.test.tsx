/**
 * Suite for the shared session-scoped registry primitive (issue #30
 * finding S3 — the single copy behind every per-tab maker and voted
 * registry wrapper).
 *
 * What is proven, falsifiably:
 * - witnessed-map semantics: set/get/has round-trip; an unwitnessed
 *   key is `undefined`/false — the primitive NEVER invents a value
 *   (UNKNOWN sentinels are wrapper vocabulary, not registry state);
 * - teardown BY CONSTRUCTION (W58-2, the !60 F2 class): creating a
 *   registry registers its clear with the session-scoped store
 *   registry, so `clearSessionScopedStores()` — the one call both
 *   session-death paths make (the 401 dual-cache teardown in
 *   `app/providers.tsx` and explicit sign-out in `modules/auth/api.ts`)
 *   — empties it with no per-module wiring to forget;
 * - reactive reads (W58-6): `useHas` re-renders on set AND on clear,
 *   so a spent affordance stays spent across remounts and dies with
 *   the session;
 * - isolation: two registries never cross-talk.
 */
import { act, render, screen } from "@testing-library/react";
import { createSessionScopedRegistry } from "../createSessionScopedRegistry";
import { clearSessionScopedStores } from "../sessionScopedStores";

describe("createSessionScopedRegistry — witnessed-map semantics", () => {
  it("round-trips set/get/has; an unwitnessed key reads undefined/false (nothing is invented)", () => {
    const registry = createSessionScopedRegistry<string, string>();
    expect(registry.get("record-1")).toBeUndefined();
    expect(registry.has("record-1")).toBe(false);

    registry.set("record-1", "user-a");
    expect(registry.get("record-1")).toBe("user-a");
    expect(registry.has("record-1")).toBe(true);

    // Overwrite: the LAST witnessed value wins (a fresh server
    // response supersedes the previous witness).
    registry.set("record-1", "user-b");
    expect(registry.get("record-1")).toBe("user-b");

    // A different key stays unwitnessed.
    expect(registry.get("record-2")).toBeUndefined();
  });

  it("clear() empties the registry", () => {
    const registry = createSessionScopedRegistry<string, true>();
    registry.set("id-1", true);
    registry.set("id-2", true);
    registry.clear();
    expect(registry.has("id-1")).toBe(false);
    expect(registry.has("id-2")).toBe(false);
  });

  it("two registries are isolated — no cross-talk between modules", () => {
    const makers = createSessionScopedRegistry<string, string>();
    const voted = createSessionScopedRegistry<string, true>();
    makers.set("shared-key", "user-a");
    expect(voted.has("shared-key")).toBe(false);
    voted.set("other-key", true);
    expect(makers.has("other-key")).toBe(false);
    makers.clear();
    expect(voted.has("other-key")).toBe(true);
  });
});

describe("createSessionScopedRegistry — teardown by construction (W58-2)", () => {
  it("clearSessionScopedStores() empties every registry created, with no per-module wiring", () => {
    // Falsifiable: if createSessionScopedRegistry stopped registering
    // its clear at construction, this witnessed state would survive the
    // session-teardown call and the assertions below would fail.
    const makers = createSessionScopedRegistry<string, string>();
    const voted = createSessionScopedRegistry<string, true>();
    makers.set("exit-1", "user-a");
    voted.set("exit-1", true);

    // The single call BOTH session-death paths make — the query-path
    // 401 dual-cache teardown (app/providers.tsx) and explicit
    // sign-out (modules/auth/api.ts) each invoke it.
    clearSessionScopedStores();

    expect(makers.get("exit-1")).toBeUndefined();
    expect(voted.has("exit-1")).toBe(false);
  });
});

describe("createSessionScopedRegistry — reactive reads (W58-6)", () => {
  function Probe({
    registry,
    id,
  }: Readonly<{
    registry: ReturnType<typeof createSessionScopedRegistry<string, true>>;
    id: string;
  }>) {
    const spent = registry.useHas(id);
    return <div data-testid="probe">{spent ? "spent" : "armed"}</div>;
  }

  it("useHas re-renders on set (arm → spent) and on clear (spent → armed)", () => {
    const registry = createSessionScopedRegistry<string, true>();
    render(<Probe registry={registry} id="vote-1" />);
    expect(screen.getByTestId("probe")).toHaveTextContent("armed");

    act(() => {
      registry.set("vote-1", true);
    });
    expect(screen.getByTestId("probe")).toHaveTextContent("spent");

    // Session teardown while mounted: the spent state DIES with the
    // session — the next operator's tab never inherits it.
    act(() => {
      clearSessionScopedStores();
    });
    expect(screen.getByTestId("probe")).toHaveTextContent("armed");
  });

  it("useHas tracks ONLY its own key", () => {
    const registry = createSessionScopedRegistry<string, true>();
    render(<Probe registry={registry} id="vote-a" />);
    act(() => {
      registry.set("vote-b", true);
    });
    expect(screen.getByTestId("probe")).toHaveTextContent("armed");
  });
});
