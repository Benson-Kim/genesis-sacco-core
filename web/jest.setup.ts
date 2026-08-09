import "@testing-library/jest-dom";
import { randomUUID } from "node:crypto";

// JSDOM doesn't expose crypto.randomUUID — polyfill it from Node's built-in
// crypto module so any code that calls it (idempotencyKeyFor, etc.) works in
// tests. (Salvaged from duo/feature/p13-5-frontend-followthrough @ 198a238.)
if (typeof (globalThis.crypto as Partial<Crypto>).randomUUID !== "function") {
  Object.defineProperty(globalThis.crypto, "randomUUID", {
    value: randomUUID,
    writable: false,
    configurable: true,
  });
}
