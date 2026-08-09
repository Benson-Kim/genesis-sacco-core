"""RBAC matrix mirrored verbatim from the prototype `seedPerms()` (P4).

7 roles x 7 prototype modules x view/create/edit/approve, deny-by-
default, plus the `corrections` module (dedicated permission
strings for the fraud channel — see _CORRECTIONS_GRANTS). Pure data:
enforcement lives in the API authz dependency and the permissions table.
"""

from __future__ import annotations

import enum


class Module(enum.StrEnum):
    MEMBERS = "members"
    APPLICATIONS = "applications"
    LOAN_BOOK = "loan_book"
    TRANSACTIONS = "transactions"
    REPORTS = "reports"
    SETTINGS = "settings"
    ACCESS_CONTROL = "access_control"
    #  extension of the prototype matrix: corrections are the
    # fraud channel, so repayment adjustments, misc fees and loan
    # write-offs carry their OWN permission strings — never generic
    # transactions:edit (maker-checker). Deliberately NARROW grants
    # (see _CORRECTIONS_GRANTS), not the generic non-admin defaults.
    CORRECTIONS = "corrections"
    # member identity is the authorization substrate for
    # member-visible money actions (credential links; the staff-attested
    # consent override), so it carries its OWN permission strings —
    # never generic members:edit. Deliberately NARROW grants (see
    # _MEMBER_IDENTITY_GRANTS), the _CORRECTIONS_GRANTS precedent.
    MEMBER_IDENTITY = "member_identity"


class Action(enum.StrEnum):
    VIEW = "view"
    CREATE = "create"
    EDIT = "edit"
    APPROVE = "approve"


SYSTEM_ADMIN = "System Admin"
BRANCH_MANAGER = "Branch Manager"
LOAN_OFFICER = "Loan Officer"
TELLER = "Teller"
CREDIT_COMMITTEE = "Credit Committee"
ACCOUNTANT = "Accountant"
AUDITOR = "Auditor"

ROLE_NAMES: tuple[str, ...] = (
    SYSTEM_ADMIN,
    BRANCH_MANAGER,
    LOAN_OFFICER,
    TELLER,
    CREDIT_COMMITTEE,
    ACCOUNTANT,
    AUDITOR,
)

#: Assurance functions (the B2 segregation-of-duties principle,
#: three lines of defense): roles whose FUNCTION is reviewing the
#: trail are excluded from acting inside the processes they audit —
#: collections assignability and the issue- maker-checker
#: CHECKER actions — even where their RBAC grants would otherwise
#: allow it (the Auditor views everything BY DESIGN, so the permission
#: matrix alone can never express this exclusion). Identified by role
#: NAME: names are the codebase's canonical role identity (the seeded
#: matrix is keyed by ROLE_NAMES and the roles table carries no
#: role-type flag column); consumers resolve the name SERVER-SIDE from
#: the actor's role_id, never from a client-supplied flag.
ASSURANCE_ROLES: frozenset[str] = frozenset({AUDITOR})

_ADMIN_MODULES = frozenset({Module.SETTINGS, Module.ACCESS_CONTROL})

RoleMatrix = dict[Module, dict[Action, bool]]

#:  (maker-checker): explicit, narrow grants for the
#: corrections module — (view, create, edit, approve) per role. The
#: MAKER (Accountant, corrections:create) posts adjustments and fees;
#: the CHECKERS (Branch Manager / Credit Committee, corrections:
#: approve) vote on and execute write-offs; the Auditor reviews. Roles
#: absent here hold NOTHING (deny by default) — the generic non-admin
#: defaults below must never leak into the fraud channel.
_CORRECTIONS_GRANTS: dict[str, tuple[bool, bool, bool, bool]] = {
    SYSTEM_ADMIN: (True, True, True, True),
    BRANCH_MANAGER: (True, False, False, True),
    CREDIT_COMMITTEE: (True, False, False, True),
    ACCOUNTANT: (True, True, False, False),
    AUDITOR: (True, False, False, False),
}

#: explicit, narrow grants for the member_identity module —
#: (view, create, edit, approve) per role. Credential link mutations
#: (member_identity:create /:edit) and the staff-attested consent
#: override (member_identity:approve) are identity-of-record powers:
#: only the roles that administer identity hold them (System Admin,
#: Branch Manager); the Auditor reviews. Roles absent here hold
#: NOTHING (deny by default, the _CORRECTIONS_GRANTS precedent) — a
#: Loan Officer's applications:edit must never imply the power to
#: re-point who a member IS or to consent in a member's name.
_MEMBER_IDENTITY_GRANTS: dict[str, tuple[bool, bool, bool, bool]] = {
    SYSTEM_ADMIN: (True, True, True, True),
    BRANCH_MANAGER: (True, True, True, True),
    AUDITOR: (True, False, False, False),
}


def _grants(role: str, module: Module) -> dict[Action, bool]:
    view = create = edit = approve = False
    if module is Module.CORRECTIONS:
        view, create, edit, approve = _CORRECTIONS_GRANTS.get(role, (False, False, False, False))
    elif module is Module.MEMBER_IDENTITY:
        view, create, edit, approve = _MEMBER_IDENTITY_GRANTS.get(
            role, (False, False, False, False)
        )
    elif role == SYSTEM_ADMIN:
        view = create = edit = approve = True
    elif role == BRANCH_MANAGER:
        view = create = edit = True
        approve = module not in _ADMIN_MODULES
    elif role == LOAN_OFFICER:
        view = module not in _ADMIN_MODULES
        create = module in {Module.MEMBERS, Module.APPLICATIONS}
        edit = module is Module.APPLICATIONS
    elif role == TELLER:
        view = module in {Module.MEMBERS, Module.TRANSACTIONS}
        create = module is Module.TRANSACTIONS
    elif role == CREDIT_COMMITTEE:
        view = module in {Module.APPLICATIONS, Module.LOAN_BOOK, Module.REPORTS}
        approve = module is Module.APPLICATIONS
    elif role == ACCOUNTANT:
        view = module in {Module.MEMBERS, Module.LOAN_BOOK, Module.TRANSACTIONS, Module.REPORTS}
        create = module is Module.TRANSACTIONS
        edit = module is Module.TRANSACTIONS
    elif role == AUDITOR:
        view = True
    return {Action.VIEW: view, Action.CREATE: create, Action.EDIT: edit, Action.APPROVE: approve}


def seed_matrix() -> dict[str, RoleMatrix]:
    """Deny-by-default matrix; only explicit grants are true."""
    return {role: {module: _grants(role, module) for module in Module} for role in ROLE_NAMES}
