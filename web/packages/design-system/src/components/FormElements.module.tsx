/**
 * FormElements — accessible, brand-token form primitives.
 *
 * Components mirror the class inventory in FormElements.module.css.
 * Each component is a thin wrapper that applies the correct CSS
 * Module class(es) and forwards all native HTML attributes so
 * callers retain full control over type, value, aria-*, etc.
 *
 * Exported surface:
 *   <FormControl>      text-like inputs (input + textarea)
 *   <SelectWrap>       native select with a CSS chevron arrow
 *   <ChoiceGroup>      fieldset wrapper for checkbox / radio groups
 *   <ChoiceList>       grid container for <Choice> items
 *   <Choice>           individual checkbox / radio card (label)
 *   <ChoiceText>       text column inside a <Choice>
 *   <Required>         asterisk marker beside a label or choice title
 *   <Hint>             helper text rendered below a control
 *   <ErrorMessage>     inline validation error with CSS "!" icon
 *   <FormStatus>       server-level error banner (role="alert")
 */
import type {
    FieldsetHTMLAttributes,
    InputHTMLAttributes,
    LabelHTMLAttributes,
    ReactNode,
    SelectHTMLAttributes,
    TextareaHTMLAttributes,
} from "react";
import styles from "./FormElements.module.css";

// ------------------------------------------------------------------ //
// FormControl — text, email, tel, url, password, number, date, …      //
// ------------------------------------------------------------------ //

export interface FormControlProps extends InputHTMLAttributes<HTMLInputElement> {
    /** Add the OTP monospace / letter-spacing modifier. */
    otp?: boolean;
}

/** Styled text-like `<input>` — apply `.formControl` + optional `.otpInput`. */
export function FormControl({ otp, className, ...rest }: Readonly<FormControlProps>) {
    const cls = [styles.formControl, otp ? styles.otpInput : "", className]
        .filter(Boolean)
        .join(" ");
    return <input {...rest} className={cls} />;
}

// ------------------------------------------------------------------ //
// FormTextarea — multiline text                                        //
// ------------------------------------------------------------------ //

export type FormTextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement>;

/** Styled `<textarea>` — applies `.formControl` with `resize: vertical`. */
export function FormTextarea({ className, ...rest }: Readonly<FormTextareaProps>) {
    const cls = [styles.formControl, className].filter(Boolean).join(" ");
    return <textarea {...rest} className={cls} />;
}

// ------------------------------------------------------------------ //
// SelectWrap — native select with CSS chevron                         //
// ------------------------------------------------------------------ //

export interface SelectWrapProps extends SelectHTMLAttributes<HTMLSelectElement> {
    children: ReactNode;
}

/**
 * Wraps a native `<select>` in the `.selectWrap` container that
 * provides the CSS-drawn chevron arrow.  Pass the `<option>` elements
 * as children.
 */
export function SelectWrap({ className, children, ...rest }: Readonly<SelectWrapProps>) {
    return (
        <div className={styles.selectWrap}>
            <select {...rest} className={[styles.formControl, className].filter(Boolean).join(" ")}>
                {children}
            </select>
        </div>
    );
}

// ------------------------------------------------------------------ //
// ChoiceGroup — fieldset for checkbox / radio groups                  //
// ------------------------------------------------------------------ //

export interface ChoiceGroupProps extends FieldsetHTMLAttributes<HTMLFieldSetElement> {
    /** Accessible group name rendered as `<legend>`. */
    legend: string;
    children: ReactNode;
}

/**
 * `<fieldset>` wrapper whose `legend` prop becomes the accessible
 * group name.  Place `<ChoiceList>` children inside.
 */
export function ChoiceGroup({
    legend,
    className,
    children,
    ...rest
}: Readonly<ChoiceGroupProps>) {
    const cls = [styles.choiceGroup, className].filter(Boolean).join(" ");
    return (
        <fieldset {...rest} className={cls}>
            <legend>{legend}</legend>
            {children}
        </fieldset>
    );
}

// ------------------------------------------------------------------ //
// ChoiceList — grid of <Choice> items                                 //
// ------------------------------------------------------------------ //

export interface ChoiceListProps {
    /** Use an auto-fit multi-column layout (`.choiceListInline`). */
    inline?: boolean;
    children: ReactNode;
}

/** Grid container for `<Choice>` cards. */
export function ChoiceList({ inline, children }: Readonly<ChoiceListProps>) {
    const cls = [styles.choiceList, inline ? styles.choiceListInline : ""]
        .filter(Boolean)
        .join(" ");
    return <div className={cls}>{children}</div>;
}

// ------------------------------------------------------------------ //
// Choice — individual checkbox / radio card                           //
// ------------------------------------------------------------------ //

export interface ChoiceProps extends LabelHTMLAttributes<HTMLLabelElement> {
    children: ReactNode;
}

/**
 * Card-style `<label>` that wraps a checkbox or radio `<input>` plus a
 * `<ChoiceText>` block.  The entire tile is the click / tap target.
 */
export function Choice({ className, children, ...rest }: Readonly<ChoiceProps>) {
    const cls = [styles.choice, className].filter(Boolean).join(" ");
    return (
        <label {...rest} className={cls}>
            {children}
        </label>
    );
}

// ------------------------------------------------------------------ //
// ChoiceText — text column inside a <Choice>                          //
// ------------------------------------------------------------------ //

export interface ChoiceTextProps {
    /** Primary bold label. Accepts nodes so a <Required> marker can be nested inside. */
    title: ReactNode;
    /** Optional muted sub-label. */
    description?: ReactNode;
}

/** Bold title + optional muted description stacked inside a `<Choice>`. */
export function ChoiceText({ title, description }: Readonly<ChoiceTextProps>) {
    return (
        <span className={styles.choiceText}>
            <span className={styles.choiceTitle}>{title}</span>
            {description !== undefined && (
                <span className={styles.choiceDescription}>{description}</span>
            )}
        </span>
    );
}

// ------------------------------------------------------------------ //
// Required — asterisk marker beside a label or choice title           //
// ------------------------------------------------------------------ //

/** Asterisk marker for required fields. Rendered `aria-hidden`; pair with `required` on the control itself. */
export function Required() {
    return (
        <span className={styles.required} aria-hidden="true">
            *
        </span>
    );
}

// ------------------------------------------------------------------ //
// Hint — helper text below a control                                  //
// ------------------------------------------------------------------ //

export interface HintProps {
    id?: string;
    children: ReactNode;
}

/** Muted helper text rendered below a form control. */
export function Hint({ id, children }: Readonly<HintProps>) {
    return (
        <p id={id} className={styles.hint}>
            {children}
        </p>
    );
}

// ------------------------------------------------------------------ //
// ErrorMessage — inline validation error                              //
// ------------------------------------------------------------------ //

export interface ErrorMessageProps {
    id?: string;
    children: ReactNode;
}

/**
 * Inline validation error with a CSS-drawn circular "!" prefix.
 * Wire the `id` to the control's `aria-describedby` for screen
 * reader announcement.
 */
export function ErrorMessage({ id, children }: Readonly<ErrorMessageProps>) {
    return (
        <p id={id} className={styles.error}>
            {children}
        </p>
    );
}

// ------------------------------------------------------------------ //
// FormStatus — server-level error banner                              //
// ------------------------------------------------------------------ //

export interface FormStatusProps {
    children: ReactNode;
}

/**
 * Full-width error banner for server-returned form errors.  Rendered
 * with `role="alert"` so screen readers announce it immediately.
 * Conditionally render this component rather than using `hidden`.
 */
export function FormStatus({ children }: Readonly<FormStatusProps>) {
    return (
        <div role="alert" className={styles.formStatus}>
            {children}
        </div>
    );
}