"use client";

/**
 * Loan products panel (prototype Settings "Loan products" tab):
 * product sidebar + optimistic-locked rule editing.
 *
 * Security posture:
 * - Product names/figures are attacker-influenced data; everything
 *   renders through React text interpolation — no parser sink.
 * - rate_pct / deposit_multiplier render VERBATIM and are submitted as
 *   decimal strings — the client never coerces or computes them
 *   .
 * - Rule changes and activation flips are money-adjacent: both flow
 *   through ConfirmDangerModal's typed confirmation (blocker
 *   (f)); every mutation carries an actor-scoped Idempotency-Key and
 *   the loaded `version` — 409 renders the shared ConflictBanner's
 *   explicit reload-and-re-enter flow (never a silent overwrite).
 * - Affordances follow the P4 matrix (settings:create/edit) as pure
 *   UX; the API enforces every call (least disclosure).
 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Button, Card, ConfirmDangerModal, Pill } from "@genesis/design-system";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { FormField } from "@/modules/forms/FormField";
import { fromApiError, fromZodError, mergeFieldErrors, type FieldErrors } from "@/modules/forms/form-errors";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { isConflict } from "@/lib/errors";
import { STALE_TIME } from "@/lib/query";
import { fetchProducts, updateProduct, type UpdateProductInput } from "../api";
import { productRulesFormSchema, type Product } from "../schemas";
import grid from "@/modules/layout/grid.module.css";
import styles from "./Products.module.css";

// Drawer-level code splitting (speed): the create drawer loads
// on first open, not with the settings route.
const ProductCreateDrawer = dynamic(
  () => import("./ProductCreateDrawer").then((m) => m.ProductCreateDrawer),
  { ssr: false },
);

export const PRODUCTS_QUERY_KEY = ["products", "list"] as const;

/**
 * staleTime is the RECORD class (0), not reference: each list row backs
 * an optimistic-locked edit (the loaded `version` must be the freshest
 * available; the 409 flow covers the rest).
 */
export function useProducts() {
  return useQuery({
    queryKey: PRODUCTS_QUERY_KEY,
    queryFn: fetchProducts,
    staleTime: STALE_TIME.record,
  });
}

export function ProductsScreen() {
  const permissions = usePermissions();
  const mayCreate = can(permissions.data, "settings", "create");
  const products = useProducts();

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  // Auto-select the first product once loaded (permissions matrix pattern).
  const autoSelected = useRef(false);
  useEffect(() => {
    if (!autoSelected.current && products.data && products.data.length > 0) {
      setSelectedId(products.data[0]!.id);
      autoSelected.current = true;
    }
  }, [products.data]);

  const selected = products.data?.find((p) => p.id === selectedId);

  return (
    <div className={grid.sideMain}>
      <Card padded={false}>
        <ul className={styles.productList} aria-label="Loan products">
          {products.isPending && <li className={styles.placeholder}>Loading…</li>}
          {products.isError && (
            <li>
              <ErrorBanner error={products.error} />
            </li>
          )}
          {(products.data ?? []).map((product) => (
            <li key={product.id}>
              <button
                type="button"
                aria-pressed={product.id === selectedId}
                className={`${styles.productItem}${product.id === selectedId ? ` ${styles.active}` : ""}`}
                onClick={() => setSelectedId(product.id)}
              >
                <span className={styles.productName}>
                  {product.name}
                  {!product.active && (
                    <Pill bg="brickSoft" color="brick">
                      Inactive
                    </Pill>
                  )}
                </span>
                <span className={styles.productSub}>
                  {product.rate_pct}% · {product.deposit_multiplier}× ·{" "}
                  {product.max_term_months}mo
                </span>
              </button>
            </li>
          ))}
          {products.data !== undefined && products.data.length === 0 && (
            <li className={styles.placeholder}>No products configured.</li>
          )}
        </ul>
        {mayCreate && (
          <div className={styles.addProduct}>
            <Button type="button" onClick={() => setCreating(true)}>
              + Add product
            </Button>
          </div>
        )}
      </Card>

      <Card padded={false}>
        {selected === undefined ? (
          <div className={styles.placeholder}>
            {products.isPending ? "Loading…" : "Select a product."}
          </div>
        ) : (
          <ProductEditPanel
            key={`${selected.id}:${selected.version}`}
            product={selected}
          />
        )}
      </Card>

      {creating && (
        <ProductCreateDrawer
          onClose={() => setCreating(false)}
          onCreated={(product) => {
            setCreating(false);
            setSelectedId(product.id);
          }}
        />
      )}
    </div>
  );
}

type ConfirmKind = null | "rules" | "active";

/**
 * Optimistic-locked rule editing (WYSIWYG: the full displayed rule set
 * is submitted with the loaded `version`). Name is IMMUTABLE in the P9
 * contract (ProductUpdateBody carries no name) and renders read-only.
 */
function ProductEditPanel({ product }: Readonly<{ product: Product }>) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const mayEdit = can(permissions.data, "settings", "edit");

  const [ratePct, setRatePct] = useState(product.rate_pct);
  const [multiplier, setMultiplier] = useState(product.deposit_multiplier);
  const [maxTerm, setMaxTerm] = useState(String(product.max_term_months));
  const [guarantors, setGuarantors] = useState(String(product.guarantors_required));
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirm, setConfirm] = useState<ConfirmKind>(null);
  const [pendingInput, setPendingInput] = useState<UpdateProductInput | null>(null);

  const rulesSlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  const activeSlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const update = useMutation({
    mutationFn: ({ input, slot }: { input: UpdateProductInput; slot: IdempotencyKeySlot }) =>
      updateProduct(
        product.id,
        input,
        idempotencyKeyFor(slot, JSON.stringify({ op: "product-update", id: product.id, input })),
      ),
    onSuccess: () => {
      setConfirm(null);
      setPendingInput(null);
      void queryClient.invalidateQueries({ queryKey: PRODUCTS_QUERY_KEY });
      announce("Product saved.");
    },
    // Close the confirmation on failure so the ConflictBanner /
    // ErrorBanner in the panel is what the operator sees; re-entering
    // the action is a NEW confirmed intent (never an auto-retry).
    onError: () => {
      setConfirm(null);
      announce("Product change failed.", "assertive");
    },
  });

  const conflict = update.isError && isConflict(update.error);
  const fieldErrors = mergeFieldErrors(clientErrors, fromApiError(update.error));

  function submitRules(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (update.isPending) return;
    const parsed = productRulesFormSchema.safeParse({
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
    // Decimal rule fields submit as STRINGS verbatim (blocker (a)).
    setPendingInput({
      version: product.version,
      rate_pct: parsed.data.rate_pct,
      deposit_multiplier: parsed.data.deposit_multiplier,
      max_term_months: parsed.data.max_term_months,
      guarantors_required: parsed.data.guarantors_required,
    });
    setConfirm("rules");
  }

  function reloadRecord() {
    // Explicit reload flow: refetch the list; this panel remounts keyed
    // on the fresh version. The stale submission is never replayed.
    void queryClient.refetchQueries({ queryKey: PRODUCTS_QUERY_KEY });
  }

  return (
    <>
      <div className={styles.panelHead}>
        <span>Edit — {product.name}</span>
        {!product.active && (
          <Pill bg="brickSoft" color="brick">
            Inactive
          </Pill>
        )}
      </div>
      <div className={styles.panelBody}>
        <ConflictBanner error={update.error} onReload={reloadRecord} />
        {update.isError && !conflict && <ErrorBanner error={update.error} />}
        <form onSubmit={submitRules} noValidate>
          <div className={styles.fieldsGrid}>
            <FormField
              id="product-rate"
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
                  disabled={!mayEdit}
                  onChange={(event) => setRatePct(event.target.value)}
                />
              )}
            </FormField>
            <FormField
              id="product-multiplier"
              label="Deposit multiplier"
              error={fieldErrors["deposit_multiplier"]}
              hint="Borrowing power = deposits × multiplier (server-computed)."
            >
              {(control) => (
                <input
                  {...control}
                  className={styles.input}
                  inputMode="decimal"
                  maxLength={6}
                  value={multiplier}
                  disabled={!mayEdit}
                  onChange={(event) => setMultiplier(event.target.value)}
                />
              )}
            </FormField>
            <FormField
              id="product-max-term"
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
                  disabled={!mayEdit}
                  onChange={(event) => setMaxTerm(event.target.value)}
                />
              )}
            </FormField>
            <FormField
              id="product-guarantors"
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
                  disabled={!mayEdit}
                  onChange={(event) => setGuarantors(event.target.value)}
                />
              )}
            </FormField>
          </div>
          <div className={styles.formNote}>
            Product name is immutable in the API contract. Optimistic lock:
            saving against record version {product.version}.
          </div>
          {mayEdit && (
            <div className={styles.actions}>
              <Button variant="primary" type="submit" disabled={update.isPending}>
                {update.isPending ? "Saving…" : "Save product"}
              </Button>
              <Button
                type="button"
                variant={product.active ? "danger" : "success"}
                disabled={update.isPending}
                onClick={() => setConfirm("active")}
              >
                {product.active ? "Deactivate" : "Reactivate"}
              </Button>
            </div>
          )}
        </form>
      </div>

      {confirm === "rules" && pendingInput !== null && (
        <ConfirmDangerModal
          title="Apply product rules"
          confirmPhrase={product.name}
          confirmLabel="Apply rules"
          pending={update.isPending}
          onClose={() => {
            if (!update.isPending) setConfirm(null);
          }}
          onConfirm={() => {
            if (!update.isPending) update.mutate({ input: pendingInput, slot: rulesSlot.current });
          }}
        >
          <div className={styles.formNote}>
            Rate, multiplier, term and guarantor rules price NEW loan
            applications from the moment they are saved. Existing loans are
            not recomputed.
          </div>
        </ConfirmDangerModal>
      )}

      {confirm === "active" && (
        <ConfirmDangerModal
          title={product.active ? "Deactivate product" : "Reactivate product"}
          confirmPhrase={product.name}
          confirmLabel={product.active ? "Deactivate product" : "Reactivate product"}
          pending={update.isPending}
          onClose={() => {
            if (!update.isPending) setConfirm(null);
          }}
          onConfirm={() => {
            if (!update.isPending)
              update.mutate({
                input: { version: product.version, active: !product.active },
                slot: activeSlot.current,
              });
          }}
        >
          <div className={styles.formNote}>
            {product.active
              ? "Deactivating stops NEW applications against this product. Existing loans are unaffected."
              : "Reactivating allows NEW applications against this product again."}
          </div>
        </ConfirmDangerModal>
      )}
    </>
  );
}
