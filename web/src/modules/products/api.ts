/**
 * Loan-products API layer (Settings module) over the GENERATED
 * client — modeled on the users module reference implementation.
 *
 * - `GET /products` returns a BOUNDED reference array (the P9 contract
 *   exposes no cursor — same non-paginated reference-data class as
 *   `GET /access/roles`); there is no offset pagination anywhere
 *   (scalability).
 * - Every mutation takes a caller-supplied Idempotency-Key following
 *   the stability/rotation contract (concurrency safety); updates send the
 *   `version` the record was loaded with — a stale write surfaces as
 *   409 (never a silent overwrite).
 * - rate_pct / deposit_multiplier are submitted as decimal STRINGS
 *   verbatim (the money rule — no client-side money math or coercion).
 */
import { toApiError } from "@genesis/api-client";
import { api } from "@/lib/api";
import { productSchema, productsSchema, type Product } from "./schemas";

export async function fetchProducts(): Promise<Product[]> {
  const { data, error, response } = await api.GET("/products");
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return productsSchema.parse(data);
}

export interface CreateProductInput {
  name: string;
  rate_pct: string;
  deposit_multiplier: string;
  max_term_months: number;
  guarantors_required: number;
}

export async function createProduct(
  input: CreateProductInput,
  idempotencyKey: string,
): Promise<Product> {
  const { data, error, response } = await api.POST("/products", {
    body: input,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return productSchema.parse(data);
}

/**
 * Partial update: absent fields stay unchanged server-side. The screen
 * submits the full rule set it displays (WYSIWYG) plus the loaded
 * `version`; activation changes travel alone via the same route.
 */
export interface UpdateProductInput {
  version: number;
  rate_pct?: string;
  deposit_multiplier?: string;
  max_term_months?: number;
  guarantors_required?: number;
  active?: boolean;
}

export async function updateProduct(
  productId: string,
  input: UpdateProductInput,
  idempotencyKey: string,
): Promise<Product> {
  const { data, error, response } = await api.PUT("/products/{product_id}", {
    params: { path: { product_id: productId } },
    body: input,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return productSchema.parse(data);
}
