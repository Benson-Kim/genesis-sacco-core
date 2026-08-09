/**
 * @genesis/design-system — the ONLY source of visual primitives and tokens
 * for the admin web app (the house doctrine). Views must compose these
 * primitives instead of introducing ad-hoc colors/spacing (reuse-first).
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
export { Field } from "./components/Field";
export {
  Choice,
  ChoiceGroup,
  ChoiceList,
  ChoiceText,
  ErrorMessage,
  FormControl,
  FormStatus,
  FormTextarea,
  Hint,
  SelectWrap,
} from "./components/FormElements.module";
export type {
  ChoiceGroupProps,
  ChoiceListProps,
  ChoiceProps,
  ChoiceTextProps,
  ErrorMessageProps,
  FormControlProps,
  FormStatusProps,
  FormTextareaProps,
  HintProps,
  SelectWrapProps,
} from "./components/FormElements.module";
export { Kv } from "./components/Kv";
export { Modal } from "./components/Modal";
export { Pill } from "./components/Pill";
export { Stat } from "./components/Stat";

