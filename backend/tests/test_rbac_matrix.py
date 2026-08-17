"""Pure matrix + spec-walk tests (P4). No database required."""

from fastapi.routing import APIRoute

from genesis.api.app import create_app
from genesis.api.authz import RequirePermission
from genesis.domain.rbac import ROLE_NAMES, Action, Module, seed_matrix

UNPROTECTED_ALLOWLIST = {
    "/healthz",
    "/readyz",
    "/auth/otp/request",
    "/auth/otp/verify",
    "/auth/refresh",
    "/auth/logout",
    "/me/permissions",
}


def test_matrix_shape() -> None:
    matrix = seed_matrix()
    assert len(matrix) == 7
    for role in ROLE_NAMES:
        # 8 modules: the 7 prototype modules + the dedicated P13.15
        # corrections module (A3 maker-checker).
        assert len(matrix[role]) == 8
        for module in Module:
            assert set(matrix[role][module]) == set(Action)


def test_matrix_matches_prototype_seed_perms() -> None:
    matrix = seed_matrix()
    for module in Module:
        assert all(matrix["System Admin"][module].values())
        assert matrix["Auditor"][module][Action.VIEW] is True
        assert matrix["Auditor"][module][Action.CREATE] is False
        assert matrix["Auditor"][module][Action.APPROVE] is False
    manager = matrix["Branch Manager"]
    assert manager[Module.MEMBERS][Action.APPROVE] is True
    assert manager[Module.SETTINGS][Action.APPROVE] is False
    assert manager[Module.ACCESS_CONTROL][Action.APPROVE] is False
    officer = matrix["Loan Officer"]
    assert officer[Module.SETTINGS][Action.VIEW] is False
    assert officer[Module.ACCESS_CONTROL][Action.VIEW] is False
    assert officer[Module.MEMBERS][Action.CREATE] is True
    assert officer[Module.APPLICATIONS][Action.EDIT] is True
    assert officer[Module.LOAN_BOOK][Action.EDIT] is False
    teller = matrix["Teller"]
    assert teller[Module.TRANSACTIONS][Action.CREATE] is True
    assert teller[Module.REPORTS][Action.VIEW] is False
    committee = matrix["Credit Committee"]
    assert committee[Module.APPLICATIONS][Action.APPROVE] is True
    assert committee[Module.TRANSACTIONS][Action.VIEW] is False
    accountant = matrix["Accountant"]
    assert accountant[Module.TRANSACTIONS][Action.EDIT] is True
    assert accountant[Module.APPLICATIONS][Action.VIEW] is False


def test_corrections_module_grants_are_narrow() -> None:
    """P13.15 A3: corrections are the fraud channel — the generic
    non-admin defaults must never leak in. Maker (Accountant: create)
    and checker (Branch Manager / Credit Committee: approve) are
    distinct roles; Teller and Loan Officer hold NOTHING. Falsifiable:
    routing corrections through the generic module defaults makes the
    Teller/Loan Officer denials and the Branch Manager create-denial
    fail."""
    matrix = seed_matrix()
    corrections = Module.CORRECTIONS
    assert all(matrix["System Admin"][corrections].values())
    maker = matrix["Accountant"][corrections]
    assert maker[Action.VIEW] and maker[Action.CREATE]
    assert not maker[Action.EDIT] and not maker[Action.APPROVE]
    for checker_role in ("Branch Manager", "Credit Committee"):
        checker = matrix[checker_role][corrections]
        assert checker[Action.VIEW] and checker[Action.APPROVE]
        assert not checker[Action.CREATE] and not checker[Action.EDIT]
    auditor = matrix["Auditor"][corrections]
    assert auditor[Action.VIEW]
    assert not any((auditor[Action.CREATE], auditor[Action.EDIT], auditor[Action.APPROVE]))
    for denied_role in ("Teller", "Loan Officer"):
        assert not any(matrix[denied_role][corrections].values())


def test_every_operation_carries_the_authz_dependency() -> None:
    """Spec walk (P4 exit): deny-by-default is structural, not a convention."""
    app = create_app()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path in UNPROTECTED_ALLOWLIST:
            continue
        calls = [dep.call for dep in route.dependant.dependencies]
        assert any(isinstance(call, RequirePermission) for call in calls), (
            f"route {route.path} lacks a RequirePermission dependency"
        )
