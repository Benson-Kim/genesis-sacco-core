import { z } from "zod";

/**
 * Keyset (cursor) page contract for all list endpoints (gate 1.3):
 * `{ items: [...], next_cursor: string | null }` with hard max page size
 * enforced server-side. Offset pagination is not supported here.
 */
export interface KeysetPage<T> {
  items: T[];
  nextCursor: string | null;
}

export function keysetPageSchema<S extends z.ZodTypeAny>(itemSchema: S) {
  return z
    .object({
      items: z.array(itemSchema),
      next_cursor: z.string().nullable(),
    })
    .transform(
      (page): KeysetPage<z.infer<S>> => ({
        items: page.items,
        nextCursor: page.next_cursor,
      }),
    );
}
