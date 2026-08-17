import { ButtonHTMLAttributes } from "react";

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'success' | 'ghost';
export type ButtonSize = 'sm' | 'md' | 'lg';

/**
 * Button — covers all NNGroup interaction states (default, hover, focus,
 * active/pressed, loading, disabled) via CSS classes in button.css.
 *
 * @param variant  Visual style. Default: 'secondary'.
 * @param size     'sm' | 'md' (default) | 'lg'.
 * @param loading  When true, shows a spinner and sets aria-busy; disables interaction
.
 */
export function Button({
  variant = 'secondary',
  size = 'md',
  loading = false,
  children,
  className,
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
}) {
  const isDisabled = disabled || loading;
  return (
    <button
      {...rest}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      className={[
        'btn',
        `btn-${variant}`,
        `btn-${size}`,
        className,
      ].filter(Boolean).join(' ')}
    >
      {loading && <span className="btn-spinner" aria-hidden="true" />}
      {children}
    </button>
  );
}
