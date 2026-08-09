/**
 * Adapter shim — maps the legacy `id` prop to `htmlFor` on the design-system
 * Field so all existing app imports keep working without callsite changes.
 * Use `Field` from `@genesis/design-system` for new code.
 */
import { Field, type FieldControlProps, type FieldProps } from "@genesis/design-system";
import type { ReactNode } from "react";

export type { FieldControlProps };

interface FormFieldProps extends Omit<FieldProps, "htmlFor"> {
  id: string;
  children: ((control: FieldControlProps) => ReactNode) | ReactNode;
}

export function FormField({ id, ...rest }: FormFieldProps) {
  return <Field htmlFor={id} {...rest} />;
}
