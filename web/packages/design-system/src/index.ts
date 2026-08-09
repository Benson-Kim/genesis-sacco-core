/**
 * @genesis/design-system — the ONLY source of visual primitives and tokens
 * for the admin web app (MASTER_PROMPT §2.3). Views must compose these
 * primitives instead of introducing ad-hoc colors/spacing (gate 1.1).
 */
export { colorTokens, cssVar, fontFamily } from "./tokens";
export type { ColorToken } from "./tokens";
export {
  DURATION_BUDGET_MS,
  MICRO_RADIUS_MAX,
  MICRO_RADIUS_MIN,
  duration,
  fontSize,
  lineHeight,
  measure,
  nestedInnerRadius,
  nestedOuterRadius,
  radius,
  spacing,
  touchTarget,
} from "./scale";
export type { FontSizeToken, RadiusToken, SpacingToken } from "./scale";
export { Banner } from "./components/Banner";
export { Button } from "./components/Button";
export { Card } from "./components/Card";
export { ConfirmDangerModal } from "./components/ConfirmDangerModal";
export { Field, type FieldControlProps, type FieldProps } from "./components/Field";
export { Input } from "./components/Input";
export type { InputProps } from "./components/Input";
export { Kv } from "./components/Kv";
export { Modal } from "./components/Modal";
export { Pill } from "./components/Pill";
export { Select } from "./components/Select";
export type { SelectProps } from "./components/Select";
export { Stat } from "./components/Stat";
export { Textarea } from "./components/Textarea";
export type { TextareaProps } from "./components/Textarea";

