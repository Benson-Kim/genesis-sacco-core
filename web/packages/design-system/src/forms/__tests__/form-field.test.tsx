/**
 * FormField + form-errors proofs (P15 Phase B). Falsifiable: drop the
 * label association, the described-by wiring, aria-invalid, or the
 * server-wins merge rule and the matching test fails.
 */
import { render, screen } from "@testing-library/react";
import { z } from "zod";
import { ApiError } from "@genesis/api-client";
import { FormField } from "../FormField";
import { fromApiError, fromZodError, mergeFieldErrors } from "../form-errors";

describe("FormField", () => {
  it("renders a PERSISTENT label associated with the control", () => {
    render(
      <FormField id="f-name" label="Full name">
        {(control) => <input {...control} />}
      </FormField>,
    );
    const input = screen.getByLabelText("Full name");
    expect(input).toHaveAttribute("id", "f-name");
    expect(input).not.toHaveAttribute("aria-invalid");
    expect(input).not.toHaveAttribute("aria-describedby");
  });

  it("wires hint AND error through aria-describedby with aria-invalid", () => {
    render(
      <FormField id="f-email" label="Email" hint="Work address." error="value is not a valid email">
        {(control) => <input {...control} />}
      </FormField>,
    );
    const input = screen.getByLabelText("Email");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAttribute("aria-describedby", "f-email-hint f-email-error");
    expect(screen.getByText("Work address.")).toHaveAttribute("id", "f-email-hint");
    const error = screen.getByRole("alert");
    expect(error).toHaveAttribute("id", "f-email-error");
    expect(error).toHaveTextContent("value is not a valid email");
  });

  it("renders hostile error text as inert React text (no sink)", () => {
    const hostile = '<img src=x onerror=alert(1)>';
    const { container } = render(
      <FormField id="f-x" label="X" error={hostile}>
        {(control) => <input {...control} />}
      </FormField>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(hostile);
    expect(container.querySelector("img")).toBeNull();
  });
});

describe("form-errors", () => {
  const schema = z.object({
    email: z.string().email(),
    name: z.string().min(1),
  });

  it("maps Zod issues to first-message-per-field", () => {
    const parsed = schema.safeParse({ email: "nope", name: "" });
    expect(parsed.success).toBe(false);
    if (parsed.success) return;
    const errors = fromZodError(parsed.error);
    expect(Object.keys(errors).sort()).toEqual(["email", "name"]);
    expect(errors.email).toBeTruthy();
  });

  it("maps ApiError 422 fields and ignores every other error shape", () => {
    const api = new ApiError(422, "validation_error", null, [
      { field: "email", message: "already registered" },
      { field: "email", message: "second message ignored" },
    ]);
    expect(fromApiError(api)).toEqual({ email: "already registered" });
    expect(fromApiError(new Error("boom"))).toEqual({});
    expect(fromApiError(new ApiError(409, "conflict", "c-1"))).toEqual({});
  });

  it("merge: the server verdict WINS per field (the API is the enforcer)", () => {
    const merged = mergeFieldErrors(
      { email: "client says invalid", name: "client only" },
      { email: "server says taken" },
    );
    expect(merged).toEqual({ email: "server says taken", name: "client only" });
  });
});
