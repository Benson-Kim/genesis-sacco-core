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
export { FilterControl, SelectFilter, SegmentFilter } from "./components/FilterControl";
export type { FilterControlProps, FilterOption } from "./components/FilterControl";
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
  Required,
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
export { Input } from "./components/Input";
export type { InputProps } from "./components/Input";
export { Kv } from "./components/Kv";
export { Modal } from "./components/Modal";
export { Pill } from "./components/Pill";
export { Select } from "./components/Select";
export type { SelectProps } from "./components/Select";
export { Stat } from "./components/Stat";
export { TabList, TabPanel } from "./components/Tabs";
export type { TabDef, TabListProps, TabPanelProps } from "./components/Tabs";
export { Textarea } from "./components/Textarea";
export type { TextareaProps } from "./components/Textarea";

// ------------------------------------------------------------------ //
// forms/ — shared form-field wiring (label/hint/error) + error mapping //
// ------------------------------------------------------------------ //
export { FormField } from "./forms/FormField";
export type { FieldControlProps } from "./forms/FormField";
export { fromApiError, fromZodError, mergeFieldErrors } from "./forms/form-errors";
export type { FieldErrors } from "./forms/form-errors";

// ------------------------------------------------------------------ //
// table/ — keyset pagination machinery (list query + table + footer)  //
// ------------------------------------------------------------------ //
export { KeysetTable } from "./table/KeysetTable";
export type { Column, KeysetTableProps } from "./table/KeysetTable";
export { useKeysetList } from "./table/useKeysetList";
export type { KeysetListResult } from "./table/useKeysetList";
export { keysetPageSchema } from "./table/schemas";
export type { KeysetPage } from "./table/schemas";
export {
  DEFAULT_PAGE_SIZE,
  KeysetPaginator,
  PAGE_SIZE_OPTIONS,
  useKeysetPagination,
} from "./table/KeysetPaginator";
export type {
  KeysetPaginationState,
  KeysetPaginatorProps,
  PageSizeOption,
} from "./table/KeysetPaginator";

// ------------------------------------------------------------------ //
// layout/ — app shell, nav, and the shared page-grid + banners         //
// ------------------------------------------------------------------ //
export { AppShell } from "./layout/AppShell";
export { Sidebar } from "./layout/Sidebar";
export { Header } from "./layout/Header";
export { NavIcon } from "./layout/NavIcon";
export type { NavIconShape } from "./layout/NavIcon";
export { NAV_SECTIONS } from "./layout/nav";
export type { NavItem, NavSection } from "./layout/nav";
export { announce, LiveAnnouncer } from "./layout/announcer";
export { ConflictBanner } from "./layout/ConflictBanner";
export { ErrorBanner } from "./layout/ErrorBanner";
export { MAIN_CONTENT_ID, SkipToContent } from "./layout/SkipToContent";
/** Shared responsive page-grid classes (`.cards4`, `.sideMain`, …) — NO private page grid in module stylesheets. */
export { default as grid } from "./layout/grid.module.css";
