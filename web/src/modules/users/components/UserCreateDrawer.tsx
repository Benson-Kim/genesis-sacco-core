"use client";

import { useRef, useState, type SubmitEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, Field, Modal } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { createUser, type CreateUserInput } from "../api";
import type { Role, User } from "../schemas";
import styles from "./Users.module.css";

/**
 * Create-user drawer. No credential is displayed or sent from this
 * console — the new user signs in through the P3 OTP flow. The
 * Idempotency-Key stays stable across retries of an identical submission
 * and rotates when the form content changes (gate 1.4).
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
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const create = useMutation({
    mutationFn: (input: CreateUserInput) =>
      createUser(input, idempotencyKeyFor(keySlot.current, JSON.stringify({ op: "create", input }))),
    onSuccess: (user) => {
      void queryClient.invalidateQueries({ queryKey: ["users", "list"] });
      queryClient.setQueryData(["users", "detail", user.id], user);
      onCreated(user);
    },
  });

  function submit(event: SubmitEvent<HTMLFormElement>) {
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
    setFormError(null);
    create.mutate(input);
  }

  return (
    <Modal title="Add user" onClose={onClose} closeDisabled={create.isPending}>
      <form onSubmit={submit}>
        <Field label="Full name" htmlFor="create-name">
          <input
            id="create-name"
            className={styles.input}
            maxLength={200}
            value={fullName}
            onChange={(event) => setFullName(event.target.value)}
          />
        </Field>
        <Field label="Email" htmlFor="create-email">
          <input
            id="create-email"
            className={styles.input}
            type="email"
            maxLength={254}
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </Field>
        <Field label="Role" htmlFor="create-role">
          <select
            id="create-role"
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
        </Field>
        <Field label="Phone (optional)" htmlFor="create-phone">
          <input
            id="create-phone"
            className={styles.input}
            maxLength={32}
            value={phone}
            onChange={(event) => setPhone(event.target.value)}
          />
        </Field>
        <Field label="Branch (optional)" htmlFor="create-branch">
          <input
            id="create-branch"
            className={styles.input}
            maxLength={120}
            value={branch}
            onChange={(event) => setBranch(event.target.value)}
          />
        </Field>
        <div className={styles.formNote}>
          The new user signs in with an OTP sent to their registered contact. No
          credential is displayed or emailed by this console.
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
