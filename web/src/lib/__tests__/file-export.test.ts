/**
 * downloadExport proofs (P15 Phase B). Falsifiable: echo the error body,
 * download despite a non-ok response, keep the object URL alive, or let
 * path separators into the filename and the matching test fails.
 */
import { ApiError } from "@genesis/api-client";
import { downloadExport } from "../file-export";

function fakeResponse(overrides: Partial<Response> & { blobData?: Blob }): Response {
  const { blobData, ...rest } = overrides;
  return {
    ok: true,
    status: 200,
    blob: () => Promise.resolve(blobData ?? new Blob(["csv,data"])),
    json: () => Promise.reject(new Error("not json")),
    ...rest,
  } as Response;
}

test("a successful export downloads via a transient revoked object URL", async () => {
  const created: Blob[] = [];
  const revoked: string[] = [];
  const clicks: HTMLAnchorElement[] = [];
  const clickSpy = jest
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(function (this: HTMLAnchorElement) {
      clicks.push(this);
    });

  await downloadExport(() => Promise.resolve(fakeResponse({})), "loans-2026-07.csv", {
    createObjectUrl: (blob) => {
      created.push(blob);
      return "blob:fake-url";
    },
    revokeObjectUrl: (url) => revoked.push(url),
  });

  expect(created).toHaveLength(1);
  expect(clicks).toHaveLength(1);
  expect(clicks[0]?.getAttribute("download")).toBe("loans-2026-07.csv");
  expect(clicks[0]?.getAttribute("href")).toBe("blob:fake-url");
  // The object URL is revoked immediately (no lingering blob reference).
  expect(revoked).toEqual(["blob:fake-url"]);
  // The transient anchor does not stay in the DOM.
  expect(document.querySelector("a[download]")).toBeNull();
  clickSpy.mockRestore();
});

test("path separators are stripped from the filename", async () => {
  const clicks: HTMLAnchorElement[] = [];
  const clickSpy = jest
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(function (this: HTMLAnchorElement) {
      clicks.push(this);
    });
  await downloadExport(() => Promise.resolve(fakeResponse({})), "../../etc/passwd", {
    createObjectUrl: () => "blob:x",
    revokeObjectUrl: () => undefined,
  });
  expect(clicks[0]?.getAttribute("download")).toBe("..-..-etc-passwd");
  clickSpy.mockRestore();
});

test("a denial surfaces as the least-disclosure ApiError — nothing downloads", async () => {
  const clickSpy = jest.spyOn(HTMLAnchorElement.prototype, "click");
  const denied = fakeResponse({
    ok: false,
    status: 403,
    json: () => Promise.resolve({ category: "forbidden", correlation_id: "corr-x" }),
  });

  const thrown = await downloadExport(() => Promise.resolve(denied), "report.csv", {
    createObjectUrl: () => "blob:never",
    revokeObjectUrl: () => undefined,
  }).catch((error: unknown) => error);

  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as ApiError).status).toBe(403);
  expect((thrown as ApiError).correlationId).toBe("corr-x");
  expect(clickSpy).not.toHaveBeenCalled();
  clickSpy.mockRestore();
});

test("a garbage error body degrades to internal_error — never echoed", async () => {
  const broken = fakeResponse({
    ok: false,
    status: 500,
    json: () => Promise.reject(new Error("not json")),
  });
  const thrown = await downloadExport(() => Promise.resolve(broken), "report.csv", {
    createObjectUrl: () => "blob:never",
    revokeObjectUrl: () => undefined,
  }).catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as ApiError).category).toBe("internal_error");
});
