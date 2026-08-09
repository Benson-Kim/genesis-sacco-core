"use client";

/**
 * Add-product drawer (Settings module — P9 POST /products).
 *
 * - FormField + form-errors wiring on every field (blocker (f)):
 *   client Zod issues merge with server 422 ApiError.fields, server
 *   verdict wins.
 * - The Idempotency-Key stays stable across retries of an identical
 *   submission and rotates when the form content changes (concurrency safety);
 *   submit is disabled while pending — double-submit yields exactly one
 *   product.
 * - A duplicate-name conflict is a CREATE conflict: no version/reload
 *   flow exists, so it renders through ErrorBanner, not ConflictBanner
 *   (finding F-B4 rationale).
 * - Decimal rule fields submit as STRINGS verbatim (blocker (a)).
 */
import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Button, Modal } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { FormField } from "@/modules/forms/FormField";
import {
  fromApiError,
  fromZodError,
  mergeFieldErrors,
  type FieldErrors,
} from "@/modules/forms/form-errors";
import { createProduct, type CreateProductInput } from "../api";
import { productCreateFormSchema, type Product } from "../schemas";
import { PRODUCTS_QUERY_KEY } from "./ProductsScreen";
import styles from "./Products.module.css";

export function ProductCreateDrawer({
  onClose,
  onCreated,
}: Readonly<{
  onClose: () => void;
  onCreated: (product: Product) => void;
}>) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [ratePct, setRatePct] = useState("");
  const [multiplier, setMultiplier] = useState("");
  const [maxTerm, setMaxTerm] = useState("");
  const [guarantors, setGuarantors] = useState("0");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const create = useMutation({
    mutationFn: (input: CreateProductInput) =>
      createProduct(
        input,
        idempotencyKeyFor(keySlot.current, JSON.stringify({ op: "product-create", input })),
      ),
    onSuccess: (product) => {
      void queryClient.invalidateQueries({ queryKey: PRODUCTS_QUERY_KEY });
      announce("Product created.");
      onCreated(product);
    },
  });

  const fieldErrors = mergeFieldErrors(clientErrors, fromApiError(create.error));

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (create.isPending) return;
    const parsed = productCreateFormSchema.safeParse({
      name: name.trim(),
      rate_pct: ratePct.trim(),
      deposit_multiplier: multiplier.trim(),
      max_term_months: maxTerm.trim(),
      guarantors_required: guarantors.trim(),
    });
    if (!parsed.success) {
      setClientErrors(fromZodError(parsed.error));
      return;
    }
    setClientErrors({});
    create.mutate({
      name: parsed.data.name,
      rate_pct: parsed.data.rate_pct,
      deposit_multiplier: parsed.data.deposit_multiplier,
      max_term_months: parsed.data.max_term_months,
      guarantors_required: parsed.data.guarantors_required,
    });
  }

  return (
    <Modal
      title="Add product"
      onClose={onClose}
      closeDisabled={create.isPending}
      // W56-3: a stray overlay click must never discard a half-completed
      // product form — dismissal is the explicit ✕ or Escape.
      dismissOnOverlay={false}
    >
      <form onSubmit={submit} noValidate>
        {create.isError && <ErrorBanner error={create.error} />}
        <FormField id="new-product-name" label="Product name" error={fieldErrors["name"]}>
          {(control) => (
            <input
              {...control}
              className={styles.input}
              maxLength={100}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          )}
        </FormField>
        <FormField
          id="new-product-rate"
          label="Rate (% p.a.)"
          error={fieldErrors["rate_pct"]}
          hint="Decimal string, max 2dp — applied by the server."
        >
          {(control) => (
            <input
              {...control}
              className={styles.input}
              inputMode="decimal"
              maxLength={6}
              value={ratePct}
              onChange={(event) => setRatePct(event.target.value)}
            />
          )}
        </FormField>
        <FormField
          id="new-product-multiplier"
          label="Deposit multiplier"
          error={fieldErrors["deposit_multiplier"]}
        >
          {(control) => (
            <input
              {...control}
              className={styles.input}
              inputMode="decimal"
              maxLength={6}
              value={multiplier}
              onChange={(event) => setMultiplier(event.target.value)}
            />
          )}
        </FormField>
        <FormField
          id="new-product-max-term"
          label="Max term (months)"
          error={fieldErrors["max_term_months"]}
        >
          {(control) => (
            <input
              {...control}
              className={styles.input}
              inputMode="numeric"
              maxLength={3}
              value={maxTerm}
              onChange={(event) => setMaxTerm(event.target.value)}
            />
          )}
        </FormField>
        <FormField
          id="new-product-guarantors"
          label="Guarantors required"
          error={fieldErrors["guarantors_required"]}
        >
          {(control) => (
            <input
              {...control}
              className={styles.input}
              inputMode="numeric"
              maxLength={2}
              value={guarantors}
              onChange={(event) => setGuarantors(event.target.value)}
            />
          )}
        </FormField>
        <div className={styles.formNote}>
          The product becomes available to NEW applications once created.
          Nothing monetary is computed here — pricing runs server-side.
        </div>
        <div className={styles.actions}>
          <Button type="button" onClick={onClose} disabled={create.isPending}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={create.isPending}>
            {create.isPending ? "Creating…" : "Create product"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
