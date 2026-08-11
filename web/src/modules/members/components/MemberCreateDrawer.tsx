"use client";
import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Button, Modal, ErrorBanner, FormField, Input, Select } from "@genesis/design-system";
import { KENYA_PHONE_MESSAGE, normalizeKenyaMsisdn } from "@/lib/phone";
import { StepRail } from "./StepRail";
import { createMember } from "../api";
import {
    MEMBER_TYPES,
    TYPE_LABELS,
    memberCreateSchema,
    type Member,
    type MemberCreateInput,
} from "../schemas";
import styles from "./Members.module.css";

/**
 * Register-member drawer (P8 POST /members). The Idempotency-Key stays
 * stable across retries of an identical submission and rotates when the
 * form content changes (concurrency safety); the submit button is disabled while
 * the mutation is pending, so a double-submit produces exactly one
 * member. Money-bearing accounts are opened by the SERVER as part of
 * member registration — nothing monetary is entered or computed here.
 */
export function MemberCreateDrawer({
    onClose,
    onCreated,
}: Readonly<{
    onClose: () => void;
    onCreated: (member: Member) => void;
}>) {
    const queryClient = useQueryClient();
    const [type, setType] = useState<string>("person");
    const [name, setName] = useState("");
    const [phone, setPhone] = useState("");
    const [email, setEmail] = useState("");
    const [nameError, setNameError] = useState<string | null>(null);
    const [emailError, setEmailError] = useState<string | null>(null);
    // blur-time Kenya-phone validation (courtesy mirror of the server rule,
    // which normalizes to E.164 on write and refuses invalid input with a
    // sanitized 422). Shows on blur, clears on correction.
    const [phoneBlurError, setPhoneBlurError] = useState<string | null>(null);
    const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

    function validatePhoneBlur(value: string) {
        const trimmed = value.trim();
        const ok = trimmed === "" || normalizeKenyaMsisdn(trimmed) !== null;
        setPhoneBlurError(ok ? null : KENYA_PHONE_MESSAGE);
    }

    const create = useMutation({
        mutationFn: (input: MemberCreateInput) =>
            createMember(
                input,
                idempotencyKeyFor(keySlot.current, JSON.stringify({ op: "member-create", input })),
            ),
        onSuccess: (member) => {
            void queryClient.invalidateQueries({ queryKey: ["members", "list"] });
            onCreated(member);
        },
    });

    function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (create.isPending) return;
        const parsed = memberCreateSchema.safeParse({
            type,
            name: name.trim(),
            phone: phone.trim() === "" ? null : phone.trim(),
            email: email.trim() === "" ? null : email.trim(),
        });
        if (!parsed.success) {
            const fieldErrors = parsed.error.flatten().fieldErrors;
            setNameError(fieldErrors.name?.[0] ?? null);
            setEmailError(fieldErrors.email?.[0] ?? null);
            return;
        }
        if (parsed.data.phone !== null && normalizeKenyaMsisdn(parsed.data.phone) === null) {
            setPhoneBlurError(KENYA_PHONE_MESSAGE);
            return;
        }
        setNameError(null);
        setEmailError(null);
        create.mutate(parsed.data);
    }

    return (
        <Modal
            title="New member registration"
            onClose={onClose}
            closeDisabled={create.isPending}
            // W56-3: a stray overlay click must never discard a half-completed
            // registration form — dismissal is the explicit ✕ or Escape.
            dismissOnOverlay={false}
        >
            {/* The identity step of the SAME four-step flow the KYC
                wizard continues (StepRail's docblock contract: both
                registration surfaces render one code-owned list). */}
            <StepRail current={0} />
            <form onSubmit={submit} noValidate>
                <FormField id="member-type" label="Member type">
                    {(control) => (
                        <Select
                            {...control}
                            value={type}
                            onChange={(event) => setType(event.target.value)}
                        >
                            {MEMBER_TYPES.map((memberType) => (
                                <option key={memberType} value={memberType}>
                                    {TYPE_LABELS[memberType]}
                                </option>
                            ))}
                        </Select>
                    )}
                </FormField>
                <FormField
                    id="member-name"
                    label="Full name"
                    error={nameError ?? undefined}
                >
                    {(control) => (
                        <Input
                            {...control}
                            maxLength={200}
                            value={name}
                            onChange={(event) => {
                                setName(event.target.value);
                                if (nameError !== null) setNameError(null);
                            }}
                        />
                    )}
                </FormField>
                <FormField
                    id="member-phone"
                    label="Phone (optional)"
                    error={phoneBlurError ?? undefined}
                    hint={phoneBlurError === null ? "+254 or 07… format accepted." : undefined}
                >
                    {(control) => (
                        <Input
                            {...control}
                            type="tel"
                            inputMode="tel"
                            autoComplete="tel"
                            maxLength={32}
                            value={phone}
                            onChange={(event) => {
                                setPhone(event.target.value);
                                if (phoneBlurError !== null) validatePhoneBlur(event.target.value);
                            }}
                            onBlur={(event) => validatePhoneBlur(event.target.value)}
                        />
                    )}
                </FormField>
                <FormField
                    id="member-email"
                    label="Email (optional)"
                    error={emailError ?? undefined}
                >
                    {(control) => (
                        <Input
                            {...control}
                            type="email"
                            inputMode="email"
                            autoComplete="email"
                            autoCapitalize="none"
                            spellCheck={false}
                            maxLength={254}
                            value={email}
                            onChange={(event) => {
                                setEmail(event.target.value);
                                if (emailError !== null) setEmailError(null);
                            }}
                        />
                    )}
                </FormField>
                {create.isError && <ErrorBanner error={create.error} />}
                <div className={styles.actions}>
                    <Button type="button" onClick={onClose} disabled={create.isPending}>
                        Cancel
                    </Button>
                    <Button type="submit" variant="primary" disabled={create.isPending}>
                        {create.isPending ? "Registering…" : "Register member"}
                    </Button>
                </div>
            </form>
        </Modal>
    );
}
