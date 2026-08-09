/**
 * File-export helper (one copy, reuse-first, for the reports/ statements modules).
 *
 * The caller performs the request through the GENERATED client (e.g.
 * `api.GET("/reports/{id}/export", { parseAs: "stream" })`) and hands the
 * raw `Response` here — no hand-written fetch exists (the house doctrine), so auth/tenant headers and URL construction stay the generated
 * client's job and nothing secret can leak into a hand-built URL.
 *
 * Failures surface as the least-disclosure ApiError ({category,
 * correlation_id} via toApiError) — the body is parsed defensively and
 * NEVER echoed (least disclosure). The download itself is a Blob object URL on a
 * transient anchor — no parser sink, revoked immediately.
 */
import { toApiError } from "@genesis/api-client";

export interface DownloadHooks {
  /** Injectable for tests — jsdom has no URL.createObjectURL. */
  createObjectUrl?: (blob: Blob) => string;
  revokeObjectUrl?: (url: string) => void;
}

export async function downloadExport(
  fetchExport: () => Promise<Response>,
  filename: string,
  hooks: DownloadHooks = {},
): Promise<void> {
  const createUrl = hooks.createObjectUrl ?? ((blob: Blob) => URL.createObjectURL(blob));
  const revokeUrl = hooks.revokeObjectUrl ?? ((url: string) => URL.revokeObjectURL(url));

  const response = await fetchExport();
  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    throw toApiError(body, response);
  }

  const blob = await response.blob();
  // Filenames are code-owned, but strip path separators defensively.
  const safeName = filename.replace(/[/\\]/g, "-");
  const url = createUrl(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = safeName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    revokeUrl(url);
  }
}
