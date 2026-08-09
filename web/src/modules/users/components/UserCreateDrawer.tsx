"use client";

import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, Modal } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { FormField } from "@/modules/forms/FormField";
import { KENYA_PHONE_MESSAGE, normalizeKenyaMsisdn } from "@/lib/phone";
import { createUser, type CreateUserInput } from "../api";
import type { Role, User } from "../schemas";
import styles from "./Users.module.css";

/**
 * Create-user drawer (salvaged from duo/feature/p13-5-frontend-followthrough @ 198a238). No credential is
 * displayed or sent from this
 * console — the new user signs in through the P3 OTP flow. The
 * Idempotency-Key stays stable across retries of an identical submission
 * and rotates when the form content changes (concurrency safety).
 */
export function UserCreateDrawer({
  roles,
  onClose,
  onCreated,
}: Readonly<{
  roles: Role[];
  onClose: () => void;
  onCreated: (user: User) => void;
}>) {
  const queryClient = useQueryClient();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [roleId, setRoleId] = useState("");
  const [phone, setPhone] = useState("");
  const [branch, setBranch] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  //  item 1 — blur-time Kenya-phone validation (courtesy mirror of
  // the server's E.164 normalization rule). Shows on blur, clears on
  // correction.
  const [phoneBlurError, setPhoneBlurError] = useState<string | null>(null);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  function validatePhoneBlur(value: string) {
    const trimmed = value.trim();
    const ok = trimmed === "" || normalizeKenyaMsisdn(trimmed) !== null;
    setPhoneBlurError(ok ? null : KENYA_PHONE_MESSAGE);
  }

  const create = useMutation({
    mutationFn: (input: CreateUserInput) =>
      createUser(input, idempotencyKeyFor(keySlot.current, JSON.stringify({ op: "create", input }))),
    onSuccess: (user) => {
      void queryClient.invalidateQueries({ queryKey: ["users", "list"] });
      queryClient.setQueryData(["users", "detail", user.id], user);
      onCreated(user);
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (create.isPending) return;
    const input: CreateUserInput = {
      full_name: fullName.trim(),
      email: email.trim(),
      role_id: roleId,
      phone: phone.trim() === "" ? null : phone.trim(),
      branch: branch.trim() === "" ? null : branch.trim(),
    };
    if (input.full_name === "" || input.email === "" || input.role_id === "") {
      setFormError("Full name, email and role are required.");
      return;
    }
    if (input.phone != null && normalizeKenyaMsisdn(input.phone) === null) {
      setPhoneBlurError(KENYA_PHONE_MESSAGE);
      return;
    }
    setFormError(null);
    create.mutate(input);
  }

  return (
    <Modal
      title="Add user"
      onClose={onClose}
      closeDisabled={create.isPending}
      // W56-3: a stray overlay click must never discard a half-completed
      // user-create form — dismissal is the explicit ✕ or Escape.
      dismissOnOverlay={false}
    >
      <form onSubmit={submit}>
        <FormField id="create-name" label="Full name">
          {(control) => (
              <input
                {...control}
                className={styles.input}
                maxLength={200}
                value={fullName}
                onChange={(event) => setFullName(event.target.value)}
              />
          )}
        </FormField>
        <FormField id="create-email" label="Email">
          {(control) => (
              <input
                {...control}
                className={styles.input}
                type="email"
                inputMode="email"
                maxLength={254}
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
          )}
        </FormField>
        <FormField id="create-role" label="Role">
          {(control) => (
              <select
                {...control}
                className={styles.select}
                value={roleId}
                onChange={(event) => setRoleId(event.target.value)}
              >
                <option value="">Select a role…</option>
                {roles.map((role) => (
                  <option key={role.id} value={role.id}>
                    {role.name}
                  </option>
                ))}
              </select>
          )}
        </FormField>
        <FormField
          id="create-phone"
          label="Phone (optional)"
          error={phoneBlurError ?? undefined}
        >
          {(control) => (
              <input
                {...control}
                className={styles.input}
                type="tel"
                inputMode="tel"
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
        <FormField id="create-branch" label="Branch (optional)">
          {(control) => (
              <input
                {...control}
                className={styles.input}
                maxLength={120}
                value={branch}
                onChange={(event) => setBranch(event.target.value)}
              />
          )}
        </FormField>
        <div className={styles.formNote}>
          The new user signs in with an OTP sent to their registered contact. No
          credential is displayed or emailed by this screen.
        </div>
        {formError !== null && <Banner variant="error">{formError}</Banner>}
        {create.isError && <ErrorBanner error={create.error} />}
        <Button
          variant="primary"
          type="submit"
          className={styles.wide}
          disabled={create.isPending}
        >
          {create.isPending ? "Creating…" : "Create user"}
        </Button>
      </form>
    </Modal>
  );
}
