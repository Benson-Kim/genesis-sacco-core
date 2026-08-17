import "@testing-library/jest-dom";
import { randomUUID } from "node:crypto";

// JSDOM doesn't expose crypto.randomUUID — polyfill it from Node's built-in
// crypto module so any code that calls it (idempotencyKeyFor, etc.) works in tests.
if (typeof (globalThis.crypto as Partial<Crypto>).randomUUID !== "function") {
  Object.defineProperty(globalThis.crypto, "randomUUID", {
    value: randomUUID,
    writable: false,
    configurable: true,
  });
}
