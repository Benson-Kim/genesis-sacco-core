import { otpCodeSchema, tokenResponseSchema } from "../schemas";

describe("auth schemas", () => {
  it("accepts a valid token response", () => {
    const parsed = tokenResponseSchema.parse({
      access_token: "a.b.c",
      refresh_token: "r".repeat(32),
      expires_in: 900,
    });
    expect(parsed.expires_in).toBe(900);
  });

  it("rejects a token response missing fields", () => {
    const result = tokenResponseSchema.safeParse({ access_token: "a.b.c" });
    expect(result.success).toBe(false);
  });

  it("accepts exactly six digits and nothing else", () => {
    expect(otpCodeSchema.safeParse("123456").success).toBe(true);
    expect(otpCodeSchema.safeParse("12345").success).toBe(false);
    expect(otpCodeSchema.safeParse("12345a").success).toBe(false);
    expect(otpCodeSchema.safeParse("1234567").success).toBe(false);
  });
});
