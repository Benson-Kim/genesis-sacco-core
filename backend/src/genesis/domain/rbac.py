"""RBAC matrix mirrored verbatim from the prototype `seedPerms()` (P4).

7 roles x 7 prototype modules x view/create/edit/approve, deny-by-
default, plus the P13.15 `corrections` module (dedicated permission
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
    # P13.15 extension of the prototype matrix: corrections are the
    # fraud channel, so repayment adjustments, misc fees and loan
    # write-offs carry their OWN permission strings — never generic
    # transactions:edit (A3 maker-checker). Deliberately NARROW grants
    # (see _CORRECTIONS_GRANTS), not the generic non-admin defaults.
    CORRECTIONS = "corrections"


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

_ADMIN_MODULES = frozenset({Module.SETTINGS, Module.ACCESS_CONTROL})

RoleMatrix = dict[Module, dict[Action, bool]]

#: P13.15 (A3 maker-checker): explicit, narrow grants for the
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


def _grants(role: str, module: Module) -> dict[Action, bool]:
    view = create = edit = approve = False
    if module is Module.CORRECTIONS:
        view, create, edit, approve = _CORRECTIONS_GRANTS.get(role, (False, False, False, False))
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
