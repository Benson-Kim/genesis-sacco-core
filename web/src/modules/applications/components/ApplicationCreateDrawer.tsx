"use client";

/**
 * Loan-application intake drawer (module 3 — prototype `vNewApp`, adapted to the P9 contract):
 * - Sends ONLY the member, product, amount, term and purpose.
 *   Rate/pricing are server-resolved from the product; the API rejects
 *   caller-sent money parameters (extra="forbid").
 * - NO live preview of installments or borrowing power: the prototype
 *   computed those locally, which blocker (a) forbids — every derived
 *   figure (cover%, max eligible) is server-computed and rendered on
 *   the created record's detail instead.
 * - The amount is a decimal STRING end-to-end (validated by shape,
 *   never parsed into a float).
 * - FormField + form-errors (blocker (f)): client Zod issues merge with
 *   server 422 ApiError.fields; the server verdict wins per field.
 * - One Idempotency-Key per logical submission (stable across retries,
 *   rotates on content change); double-submit short-circuits (concurrency safety).
 */
import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import {
  Button,
  Modal,
  FormField,
  fromApiError,
  fromZodError,
  mergeFieldErrors,
  type FieldErrors,
  ErrorBanner,
  ErrorMessage,
  announce,
  Input,
  Select,
  Textarea,
} from "@genesis/design-system";
import type { Member } from "@/modules/members/schemas";
import { MemberLookupField } from "@/modules/members/components/MemberLookupField";
import { createApplication } from "../api";
import {
  applicationCreateSchema,
  type Application,
  type ApplicationCreateInput,
  type Product,
} from "../schemas";
import styles from "./Applications.module.css";

export function ApplicationCreateDrawer({
  products,
  onClose,
  onCreated,
}: Readonly<{
  products: Product[];
  onClose: () => void;
  onCreated: (application: Application) => void;
}>) {
  const queryClient = useQueryClient();
  const [productId, setProductId] = useState("");
  const [amount, setAmount] = useState("");
  const [term, setTerm] = useState("");
  const [purpose, setPurpose] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  // Member picker: ONE exact-match lookup by the identifier the operator
  // types (number or national ID) — never a paged dump of the whole
  // membership into a <select>. The resolved member IS the selection.
  const [member, setMember] = useState<Member | null>(null);
  const memberId = member?.id ?? "";

  const create = useMutation({
    mutationFn: (input: ApplicationCreateInput) =>
      createApplication(
        input,
        idempotencyKeyFor(keySlot.current, JSON.stringify({ op: "application-create", input })),
      ),
    onSuccess: (application) => {
      void queryClient.invalidateQueries({ queryKey: ["applications", "list"] });
      announce("Application submitted.");
      onCreated(application);
    },
  });

  const activeProducts = products.filter((product) => product.active);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (create.isPending) return;
    const termNumber = Number(term.trim());
    const parsed = applicationCreateSchema.safeParse({
      member_id: memberId,
      product_id: productId,
      amount: amount.trim(),
      term_months: term.trim() === "" ? Number.NaN : termNumber,
      purpose: purpose.trim() === "" ? null : purpose.trim(),
    });
    if (!parsed.success) {
      setClientErrors(fromZodError(parsed.error));
      return;
    }
    setClientErrors({});
    create.mutate(parsed.data);
  }

  // Server 422 verdicts WIN over the client's guesses per field.
  const serverErrors = fromApiError(create.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  // A 422 whose messages all rendered inline needs no duplicate banner;
  // anything else (or a fieldless 422) surfaces the least-disclosure banner.
  const renderedInline =
    create.error instanceof ApiError &&
    create.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  return (
    <Modal
      title="New loan application"
      onClose={onClose}
      closeDisabled={create.isPending}
      // W56-3: a stray overlay click must never discard a half-completed
      // intake form — dismissal is the explicit ✕ or Escape.
      dismissOnOverlay={false}
    >
      <form onSubmit={submit} noValidate>
        <MemberLookupField
          idPrefix="app-member"
          hint="Search by member number or national ID"
          onResolved={setMember}
        />
        {fieldErrors["member_id"] !== undefined && (
          <ErrorMessage id="app-member-error">{fieldErrors["member_id"]}</ErrorMessage>
        )}
        <FormField id="app-product" label="Loan product" error={fieldErrors["product_id"]}>
          {(control) => (
            <Select
              {...control}
              value={productId}
              onChange={(event) => setProductId(event.target.value)}
            >
              <option value="">Select a product…</option>
              {activeProducts.map((product) => (
                <option key={product.id} value={product.id}>
                  {product.name}
                </option>
              ))}
            </Select>
          )}
        </FormField>
        <FormField
          id="app-amount"
          label="Amount (KES)"
          error={fieldErrors["amount"]}
          hint="Decimal amount, e.g. 250000 or 250000.00"
        >
          {(control) => (
            <Input
              {...control}
              inputMode="decimal"
              maxLength={18}
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
            />
          )}
        </FormField>
        <FormField id="app-term" label="Term (months)" error={fieldErrors["term_months"]}>
          {(control) => (
            <Input
              {...control}
              inputMode="numeric"
              maxLength={3}
              value={term}
              onChange={(event) => setTerm(event.target.value)}
            />
          )}
        </FormField>
        <FormField id="app-purpose" label="Purpose (optional)" error={fieldErrors["purpose"]}>
          {(control) => (
            <Textarea
              {...control}
              maxLength={500}
              value={purpose}
              onChange={(event) => setPurpose(event.target.value)}
            />
          )}
        </FormField>
        {create.isError && !renderedInline && <ErrorBanner error={create.error} />}
        <div className={styles.actions}>
          <Button type="button" onClick={onClose} disabled={create.isPending}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={create.isPending}>
            {create.isPending ? "Submitting…" : "Submit application"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
