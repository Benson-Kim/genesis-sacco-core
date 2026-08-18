#!/usr/bin/env python3
"""
Genesis Prestige -- volume dev-data generator
=============================================
Drives the live API (never bypasses business rules) to create:
  - 200 members (person/company/group) across 5 branches
  - 14 months of monthly deposits + share top-ups per member
  - ~80 loan applications -> appraisal -> committee -> disbursement
  - Repayments on disbursed loans
  - Deposit interest accrual job

Usage (from backend/ directory, venv active):
  python scripts/generate_dev_data.py

Prerequisites:
  pip install httpx faker rich   (psycopg is already a backend dependency)
  seed_dev.sql already run   (provides tenant, roles, users, products)
  uvicorn running:
    uvicorn genesis.api.app:create_app --factory --reload --port 8000 --env-file .env

The script is IDEMPOTENT: re-running skips completed phases via a local
state file (dev_data_state.json).  Delete it to regenerate from scratch.

Environment overrides:
  API_BASE_URL      default http://localhost:8000
  DATABASE_URL      default postgresql://genesis:genesis@localhost:5432/genesis
  TENANT_ID         default 11111111-1111-1111-1111-111111111111
  ADMIN_EMAIL       default admin@genesisprestige.test
  LOAN_OFFICER_EMAIL default antony.maina@dev.test
  COMMITTEE_EMAIL   default board.member.a@dev.test
  NUM_MEMBERS       default 200
  NUM_LOAN_MEMBERS  default 80
  MONTHS_HISTORY    default 14
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Third-party imports -- guarded so a missing dev package fails with a
# friendly, timestamped install hint (the cron_*.py scripts' logging
# convention) instead of a bare traceback.
# ---------------------------------------------------------------------------
try:
    import httpx
    import psycopg
    from faker import Faker
    from rich.console import Console
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
except ModuleNotFoundError as exc:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger(Path(__file__).stem).error(
        "missing package: %s -- install with: pip install httpx faker rich "
        "(psycopg ships with the backend package)",
        exc.name,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://genesis:genesis@localhost:5432/genesis")
# psycopg needs a plain libpq URL; tolerate the SQLAlchemy driver scheme.
_DSN = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)
TENANT_ID = os.getenv("TENANT_ID", "11111111-1111-1111-1111-111111111111")
OTP_PEPPER = os.getenv("OTP_PEPPER", "dev-only-otp-pepper-change-me")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@genesisprestige.test")
LOAN_OFFICER_EMAIL = os.getenv("LOAN_OFFICER_EMAIL", "antony.maina@dev.test")
COMMITTEE_EMAIL = os.getenv("COMMITTEE_EMAIL", "board.member.a@dev.test")

NUM_MEMBERS = int(os.getenv("NUM_MEMBERS", "200"))
NUM_LOAN_MEMBERS = int(os.getenv("NUM_LOAN_MEMBERS", "80"))
MONTHS_HISTORY = int(os.getenv("MONTHS_HISTORY", "14"))

STATE_FILE = Path(__file__).parent / "dev_data_state.json"

fake = Faker("en_GB")
Faker.seed(42)

# Deterministic randomness for the generated volume data. This is a DEV
# SEEDER driving fake records through the live API: reproducibility (a
# fixed seed) is the requirement and cryptographic strength (S311's
# concern) deliberately is NOT -- nothing security-sensitive is drawn
# from this generator; the OTP path below uses hmac/sha256. The single
# suppression lives here so every draw goes through this one instance.
_RNG = random.Random(42)  # noqa: S311 -- dev seeder, not crypto (see above)

# On Windows the default cp1252 console cannot encode Rich's Unicode spinner
# characters.  Force UTF-8 on stdout/stderr and tell Rich to use the plain
# ASCII-safe "dots" spinner so the progress bar renders everywhere.
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

console = Console(highlight=False)
_SPINNER = "dots" if sys.platform != "win32" else "line"


# ---------------------------------------------------------------------------
# State persistence -- allows resume on failure
# ---------------------------------------------------------------------------
class State:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._path.write_text(json.dumps(self._data, indent=2))

    def __contains__(self, key: str) -> bool:
        return key in self._data


# ---------------------------------------------------------------------------
# OTP injection -- inserts a known challenge directly in the database
# (dev only)
# ---------------------------------------------------------------------------
KNOWN_CODE = "999999"  # fixed dev OTP; never used in production


def _inject_otp(email: str) -> bool:
    """Write a known OTP challenge row for `email`.

    Uses the same hash_code() logic as genesis/domain/otp.py:
      hmac(pepper, f"{challenge_id}:{code}", sha256)

    Connects with psycopg and PARAMETERIZED statements -- no string-built
    SQL, no shelling out to psql.  RLS is FORCED on otp_challenges, so
    set_config('app.tenant_id', ..., true) scopes the transaction and the
    policy is satisfied without needing BYPASSRLS.

    Returns True on success.
    """
    challenge_id = str(uuid.uuid4())
    code_hash = hmac.new(
        OTP_PEPPER.encode(),
        f"{challenge_id}:{KNOWN_CODE}".encode(),
        hashlib.sha256,
    ).hexdigest()

    try:
        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id::text, id FROM users WHERE email = %s LIMIT 1",
                (email,),
            )
            row = cur.fetchone()
            if row is None:
                console.print(f"[red]OTP inject failed: user not found: {email}[/red]")
                return False
            tenant_id, user_id = row
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            cur.execute("DELETE FROM otp_challenges WHERE user_id = %s", (user_id,))
            cur.execute(
                "INSERT INTO otp_challenges (id, tenant_id, user_id, code_hash, expires_at) "
                "VALUES (%s, %s::uuid, %s, %s, now() + interval '10 minutes')",
                (challenge_id, tenant_id, user_id, code_hash),
            )
    except psycopg.Error as exc:
        console.print(f"[red]OTP inject failed for {email}:[/red] {exc}")
        return False
    return True


# ---------------------------------------------------------------------------
# HTTP client with auth token management
# ---------------------------------------------------------------------------
class GenesisClient:
    def __init__(self, base_url: str, tenant_id: str) -> None:
        self._base = base_url.rstrip("/")
        self._tid = tenant_id
        self._token: str | None = None
        self._http = httpx.Client(timeout=30.0)

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"x-tenant-id": self._tid}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def login(self, email: str) -> None:
        # 1. Request OTP through the API (creates challenge row via service)
        r = self._http.post(
            f"{self._base}/auth/otp/request",
            json={"email": email},
            headers={"x-tenant-id": self._tid},
        )
        r.raise_for_status()

        # 2. Overwrite the challenge row with our known code (dev only)
        if not _inject_otp(email):
            raise RuntimeError(f"OTP injection failed for {email}")

        # 3. Verify with the known code
        r = self._http.post(
            f"{self._base}/auth/otp/verify",
            json={"email": email, "code": KNOWN_CODE},
            headers={
                "x-tenant-id": self._tid,
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        if r.status_code != 200:
            raise RuntimeError(f"OTP verify failed for {email}: {r.status_code} {r.text[:200]}")
        self._token = r.json()["access_token"]

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._http.get(f"{self._base}{path}", headers=self._headers(), **kwargs)

    def post(self, path: str, *, idem: bool = True, **kwargs: Any) -> httpx.Response:
        h = self._headers()
        if idem:
            h["Idempotency-Key"] = str(uuid.uuid4())
        return self._http.post(f"{self._base}{path}", headers=h, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._http.put(f"{self._base}{path}", headers=self._headers(), **kwargs)

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------
def _body(r: httpx.Response, *, allow_409: bool = True) -> dict[str, Any] | None:
    """Return parsed JSON, None on 409 (already exists), raise on other errors."""
    if r.status_code == 409 and allow_409:
        return None
    if r.status_code >= 400:
        console.print(
            f"[red]HTTP {r.status_code}[/red] {r.request.method} {r.url}\n  {r.text[:300]}"
        )
        raise RuntimeError(f"API {r.status_code}")
    return r.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Phase 0 -- Health check + login
# ---------------------------------------------------------------------------
def phase_login() -> tuple[GenesisClient, GenesisClient, GenesisClient]:
    console.rule("[bold]Phase 0 - Authentication")

    try:
        httpx.get(f"{API_BASE}/healthz", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        console.print(f"[red]API not reachable at {API_BASE}: {exc}[/red]")
        console.print(
            "Start with:  uvicorn genesis.api.app:create_app "
            "--factory --reload --port 8000 --env-file .env"
        )
        sys.exit(1)

    roles = [
        ("admin", ADMIN_EMAIL),
        ("loan_officer", LOAN_OFFICER_EMAIL),
        ("committee", COMMITTEE_EMAIL),
    ]
    clients: list[GenesisClient] = []
    for label, email in roles:
        c = GenesisClient(API_BASE, TENANT_ID)
        c.login(email)
        console.print(f"  [green]OK[/green] {label} ({email})")
        clients.append(c)

    return clients[0], clients[1], clients[2]


# ---------------------------------------------------------------------------
# Phase 1 -- Create members
# ---------------------------------------------------------------------------
_TYPES = ["person"] * 16 + ["company"] * 2 + ["group"] * 2


def phase_members(admin: GenesisClient, state: State) -> list[str]:
    console.rule("[bold]Phase 1 - Members")

    if "member_ids" in state:
        ids: list[str] = state.get("member_ids")
        console.print(f"  -- skipped ({len(ids)} already in state)")
        return ids

    member_ids: list[str] = []

    with Progress(
        SpinnerColumn(_SPINNER),
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("Creating members", total=NUM_MEMBERS)
        for i in range(NUM_MEMBERS):
            mtype = _RNG.choice(_TYPES)
            if mtype == "person":
                name = fake.name()
            elif mtype == "company":
                name = fake.company()
            else:
                name = f"{fake.word().capitalize()} Chama"
            payload: dict[str, Any] = {
                "type": mtype,
                "name": name[:200],
                "phone": f"+2547{_RNG.randint(10_000_000, 99_999_999)}",
                "email": fake.unique.email(),
            }
            r = admin.post("/members", json=payload)
            b = _body(r)
            if b:
                member_ids.append(b["id"])
            prog.advance(task)
            if i % 25 == 24:
                time.sleep(0.1)  # gentle throttle

    state.set("member_ids", member_ids)
    console.print(f"  OK {len(member_ids)} members created")
    return member_ids


# ---------------------------------------------------------------------------
# Phase 2 -- One deposit + share top-up per member in the current month
# (Historical months are handled by seed_dev.sql which inserts directly.
#  The API only posts to the open/current period, so we do one round here.)
# ---------------------------------------------------------------------------
def phase_transactions(admin: GenesisClient, member_ids: list[str], state: State) -> None:
    console.rule("[bold]Phase 2 - Current-month deposits & share top-ups")

    if state.get("transactions_done"):
        console.print("  -- skipped")
        return

    channels = ["mpesa", "bank"]
    total = len(member_ids) * 2  # one deposit + one share top-up each

    t0 = time.monotonic()
    with Progress(
        SpinnerColumn(_SPINNER),
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("Posting transactions", total=total)
        for i, mid in enumerate(member_ids):
            ch = _RNG.choice(channels)

            dep = str(round(_RNG.uniform(1000, 10_000), 2))
            r = admin.post(
                f"/members/{mid}/deposits",
                json={"amount": dep, "channel": ch},
            )
            _body(r)
            prog.advance(task)

            sha = str(round(_RNG.uniform(500, 5_000), 2))
            r = admin.post(
                f"/members/{mid}/share-topups",
                json={"amount": sha, "channel": ch},
            )
            _body(r)
            prog.advance(task)

            if i % 10 == 9:
                time.sleep(0.02)

    elapsed = time.monotonic() - t0
    console.print(
        f"  OK {total} transactions posted in {elapsed:.1f}s ({total / elapsed:.0f} txn/s)"
    )
    state.set("transactions_done", True)


# ---------------------------------------------------------------------------
# Phase 3 -- Loan applications -> committee -> disbursement -> repayments
# ---------------------------------------------------------------------------
def phase_loans(
    admin: GenesisClient,
    loan_officer: GenesisClient,
    committee: GenesisClient,
    member_ids: list[str],
    state: State,
) -> None:
    console.rule("[bold]Phase 3 - Loans")

    if state.get("loans_done"):
        console.print("  -- skipped")
        return

    # Fetch product list (loan_officer has applications:create -> can list products)
    r = loan_officer.get("/products")
    r.raise_for_status()
    products: list[dict[str, Any]] = r.json()
    if not products:
        console.print("[yellow]  ! No products found -- run seed_dev.sql first[/yellow]")
        return

    borrowers = _RNG.sample(member_ids, min(NUM_LOAN_MEMBERS, len(member_ids)))

    created = disbursed = 0
    purposes = [
        "Business expansion",
        "School fees",
        "Medical expenses",
        "Home improvement",
        "Equipment purchase",
        "General purpose",
        "Salary advance",
        "Agricultural input",
    ]

    with Progress(
        SpinnerColumn(_SPINNER),
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("Processing loans", total=len(borrowers))

        for mid in borrowers:
            product = _RNG.choice(products)
            amount = str(round(_RNG.uniform(20_000, 500_000), 2))
            max_term = product.get("max_term_months", 48)
            term = _RNG.choice([t for t in [6, 12, 24, 36, 48] if t <= max_term] or [max_term])

            # --- 1. Create application (loan officer) ---
            r = loan_officer.post(
                "/applications",
                json={
                    "member_id": mid,
                    "product_id": product["id"],
                    "amount": amount,
                    "term_months": term,
                    "purpose": _RNG.choice(purposes),
                },
            )
            app = _body(r)
            if not app:
                prog.advance(task)
                continue
            app_id = app["id"]
            ver = app["version"]
            created += 1

            # --- 2. Appraisal (loan officer) ---
            r = loan_officer.post(
                f"/applications/{app_id}/transition",
                json={"version": ver, "target": "appraisal"},
            )
            app = _body(r)
            if not app:
                prog.advance(task)
                continue
            ver = app["version"]

            # --- 3. Committee (loan officer recommends) ---
            r = loan_officer.post(
                f"/applications/{app_id}/transition",
                json={"version": ver, "target": "committee"},
            )
            app = _body(r)
            if not app:
                prog.advance(task)
                continue

            # --- 4. Votes: committee + admin have applications:approve.
            #         quorum=2 so two votes suffice.  Skip 403 (voter lacks
            #         the permission) and 409 (already voted) gracefully.
            voters = [committee, admin]
            approved = False
            for voter in voters:
                r = voter.post(
                    f"/applications/{app_id}/vote",
                    json={"vote": "approve"},
                )
                if r.status_code == 403:
                    continue  # this user lacks approve rights -- skip
                result = _body(r, allow_409=True)
                if result and result.get("decision") == "approved":
                    approved = True
                    break

            if not approved:
                prog.advance(task)
                continue

            # --- 5. Disburse (admin, who has loan_book:create) ---
            r = admin.post(
                f"/applications/{app_id}/disburse",
                json={"channel": "bank"},
            )
            loan = _body(r)
            if not loan:
                prog.advance(task)
                continue

            loan_id = loan["loan_id"]
            disbursed += 1

            # --- 6. Post a few repayments ---
            months_paid = _RNG.randint(1, min(6, term))
            inst = round(float(amount) / term, 2)
            for _ in range(months_paid):
                repayment = str(round(inst * _RNG.uniform(0.95, 1.05), 2))
                r = admin.post(
                    f"/loans/{loan_id}/repayments",
                    json={"amount": repayment, "channel": "mpesa"},
                )
                _body(r, allow_409=True)

            prog.advance(task)
            time.sleep(0.03)

    state.set("loans_done", True)
    console.print(f"  OK {created} applications | {disbursed} disbursed")


# ---------------------------------------------------------------------------
# Phase 4 -- Deposit interest accrual
# ---------------------------------------------------------------------------
def phase_interest(admin: GenesisClient, state: State) -> None:
    console.rule("[bold]Phase 4 - Deposit interest accrual")

    if state.get("interest_done"):
        console.print("  -- skipped")
        return

    r = admin.post("/jobs/deposit-interest", json={})
    b = _body(r, allow_409=True)
    if b:
        console.print(
            f"  OK period {b.get('period_start')} -> {b.get('period_end')} | "
            f"posted={b.get('posted')} | total_interest={b.get('total_interest')}"
        )
    else:
        console.print("  OK already accrued (no-op)")

    state.set("interest_done", True)


# ---------------------------------------------------------------------------
# Phase 5 -- Arrears job (classify loans, accrue penalties)
# ---------------------------------------------------------------------------
def phase_arrears(admin: GenesisClient, state: State) -> None:
    console.rule("[bold]Phase 5 - Arrears classification")

    if state.get("arrears_done"):
        console.print("  -- skipped")
        return

    r = admin.post("/jobs/arrears", json={})
    b = _body(r, allow_409=False)
    if b:
        console.print(
            f"  OK scanned={b.get('scanned')} | updated={b.get('updated')} | "
            f"penalty_configured={b.get('penalty_configured')}"
        )

    state.set("arrears_done", True)


# ---------------------------------------------------------------------------
# Phase 6 -- Print summary stats
# ---------------------------------------------------------------------------
def phase_summary(admin: GenesisClient) -> None:
    console.rule("[bold]Summary")
    checks = [
        ("/members?limit=1", "Members"),
        ("/transactions?limit=1", "Transactions"),
        ("/applications?limit=1", "Applications"),
        ("/loans?limit=1", "Loans"),
        ("/portfolio/summary", "Portfolio"),
    ]
    for path, label in checks:
        r = admin.get(path)
        if r.status_code == 200:
            b = r.json()
            if "next_cursor" in b:
                has_more = b["next_cursor"] is not None
                console.print(f"  {label}: >=1 record {'(more)' if has_more else ''}")
            else:
                # Portfolio summary or similar flat response
                console.print(f"  {label}: {json.dumps(b)[:120]}")
        else:
            console.print(f"  [yellow]{label}: HTTP {r.status_code}[/yellow]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    console.print("[bold green]Genesis Prestige -- Volume Data Generator[/bold green]")
    console.print(f"  API:           {API_BASE}")
    console.print(f"  Tenant:        {TENANT_ID}")
    console.print(f"  Members:       {NUM_MEMBERS}")
    console.print(f"  Loan members:  {NUM_LOAN_MEMBERS}")
    console.print(f"  History:       {MONTHS_HISTORY} months")
    console.print(f"  State file:    {STATE_FILE}\n")

    state = State(STATE_FILE)
    admin, loan_officer, committee = phase_login()

    try:
        member_ids = phase_members(admin, state)
        phase_transactions(admin, member_ids, state)
        phase_loans(admin, loan_officer, committee, member_ids, state)
        phase_interest(admin, state)
        phase_arrears(admin, state)
        phase_summary(admin)
    finally:
        admin.close()
        loan_officer.close()
        committee.close()

    console.print(
        f"\n[bold green]OK Done.[/bold green]  "
        f"State saved -> {STATE_FILE}\n"
        "Delete it to regenerate from scratch."
    )


if __name__ == "__main__":
    main()
