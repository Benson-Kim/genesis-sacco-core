/**
 * @genesis/design-system — the ONLY source of visual primitives and tokens
 * for the admin web app (MASTER_PROMPT §2.3). Views must compose these
 * primitives instead of introducing ad-hoc colors/spacing (gate 1.1).
 */
export { colorTokens, cssVar, fontFamily } from "./tokens";
export type { ColorToken } from "./tokens";
export { Banner } from "./components/Banner";
export { Button } from "./components/Button";
export { Card } from "./components/Card";
export { Field } from "./components/Field";
export { Modal } from "./components/Modal";
export { Pill } from "./components/Pill";
export { Stat } from "./components/Stat";
