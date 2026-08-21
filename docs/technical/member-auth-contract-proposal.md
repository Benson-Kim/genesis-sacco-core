# Member sign-in: PIN + OTP — contract proposal

**Status:** proposal, awaiting the backend author.
**Written:** 2026-08-21. **Audience:** whoever builds the member auth surface.

The mobile client has the screens for this flow built and rendered. Nothing
behind them exists. This is the contract the client currently assumes, written
down so it can be corrected *before* either side is committed to it — the
cheapest moment to change it.

Everything here lives under `/member/`, so the client's path guard and the CI
staff-path sweep cover it exactly as they cover the merged routes.

---

## The flow

1. Member enters **member number + PIN**.
2. Server verifies the PIN and dispatches an **OTP** to the registered phone
   and/or email.
3. Member enters the code.
4. Server issues the member-audience token pair.

The PIN is the first factor, the OTP the second. That ordering is what the
owner described on 2026-08-21, and it is stronger than a PIN alone — but note
it changes the PIN's role from the 2026-08-20 sign-off, where the PIN was a
step-up *on an existing OTP session*. Worth confirming both records now agree.

---

## Two things that must not be got wrong

### 1. A wrong PIN and an unknown member number must be indistinguishable

Member numbers run in sequence. If sign-in answers differently for "no such
member" and "wrong PIN", the login screen becomes a membership enumerator:
walk the numbers, note which ones say *wrong PIN*, and you have the register.

That is a worse oracle than the one gate 1.6 already prevents on the OTP
route, because email addresses are not guessable in sequence and member
numbers are. It is also invisible unless it is designed for from the start —
timing included, since a PIN comparison that only runs for real members leaks
the same fact through latency.

**The client cannot compensate for this.** It renders one message either way
because it is never told which happened.

### 2. A 4-digit PIN is 10,000 combinations

As a *first* factor that is only safe behind server-side attempt counting and
lockout. Without it, an attacker with one member number walks the whole space.
The OTP behind it limits the blast radius but does not make the PIN sound: a
PIN that becomes known stays useful for every future session.

**Recommendation on the record: six digits.** The concept drawings showed
four. The client treats length as a build-time constant (`Flavor.pinLength`,
currently 6) rather than a literal, so either works — but the choice should be
deliberate. If it is four, the lockout policy is not optional.

---

## Proposed shapes

### `POST /member/auth/sign-in`

```json
{ "member_no": "GP-00123", "pin": "246810" }
```

**200**

```json
{
  "challenge_id": "opaque",
  "destination": "07XX XXX 678",
  "expires_at": "2026-08-21T09:05:00Z"
}
```

- `destination` is **masked by the server**. The client must never be sent a
  full number to mask itself — a client that masks a value is a client that
  was given it. Showing it here is safe precisely because the PIN has already
  been verified.
- `expires_at` is absolute, not a duration, so a countdown cannot drift with a
  slow round trip. `OTP_TTL_SECONDS` is 300 today and is not returned by
  anything; the client would otherwise have to hardcode that constant and show
  the wrong number from the day someone tunes it.

**401** — wrong PIN **or** unknown member number. One response, one body.
**403** — locked out. The one distinguishable failure, because a member who
cannot get in needs to be told to call the SACCO rather than left retrying.
**429** — rate limited.

### `POST /member/auth/challenge/verify`

```json
{ "challenge_id": "opaque", "code": "123456" }
```

**200** — `TokenResponse`, the same shape the merged routes already return.
**401** — wrong or expired code.

### `POST /member/auth/challenge/resend`

```json
{ "challenge_id": "opaque" }
```

**200** — a fresh challenge, because the expiry moves and a countdown against
the old one counts to the wrong moment.

### `POST /member/auth/pin/reset/request`

```json
{ "member_no": "GP-00123" }
```

**200** — challenge, **without `destination`**.

This route cannot verify anything first, so it is back where the OTP-only
route is: it must answer identically whether or not the member number exists,
and naming the destination would confirm it is real. The client's copy hedges
accordingly — *"if that member number is registered…"*.

### `POST /member/auth/pin/reset`

```json
{ "challenge_id": "opaque", "code": "123456", "pin": "285193" }
```

**204.**

Code and new PIN in **one call**, deliberately. Verifying the code as its own
round trip would create a window in which a verified code is spendable alone,
and a verified code that grants a PIN change *is* a credential.

### `POST /member/auth/pin/initial`

Same body, returns `TokenResponse`. For a member signing in for the first time
who has no PIN yet. The client never decides that a member is new — it needs
the server to say so, and the shape for that is **open** (see below).

---

## Open questions

| # | Question | Why the client cares |
|---|---|---|
| 1 | **Member number format?** | Client validation is deliberately permissive (letters, digits, `-`, `/`, 3–20 chars) because a strict guess locks out real members. Tighten it once the format is known. |
| 2 | **PIN length: 4 or 6?** | Security decision, not layout. See above. |
| 3 | **How does a member get their first PIN?** | Set at the branch during onboarding, or self-set on first sign-in via `pin/initial`? If the latter, how does the server signal "this member has no PIN" without revealing that the member exists? |
| 4 | **Lockout policy** — how many attempts, how long, and how is it cleared? | The client tells a locked-out member to call the SACCO. If it self-clears after an interval, saying so would be kinder. |
| 5 | **Is `Idempotency-Key` honoured on these routes?** | The client sends one on all of them. Sign-in is a mutation — it burns an attempt and may send a paid SMS — so a double tap must not do either twice. |
| 6 | **Does the OTP go to phone, email, or both?** | Affects the wording of `destination` and whether the member gets a choice. |
| 7 | **Is `expires_at` acceptable to return?** | Without it there is no honest countdown. |

---

## What the client has already built

| Screen | State |
|---|---|
| Member number + PIN, with reveal and Forgot PIN | Built, rendered |
| Code step, with masked destination and live countdown | Built, rendered |
| Forgot PIN: number → code → new PIN → done | Built, rendered |
| First-PIN setup | Port method exists; screen pending question 3 |
| Onboarding carousel | Built, rendered, needs no backend |

All of it sits behind `AuthMode.pinThenOtp`, a build-time flavor constant. No
shipping build selects it: the merged OTP-only flow is what ships until these
endpoints exist. When they land, the work is `MemberCredentialRepository`
matching reality and one flavor flipped.
