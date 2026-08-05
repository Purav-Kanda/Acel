"""Tests for shadow mode: violations are detected and recorded, but never
block execution. This is the mode a new deployment is meant to onboard with.
"""

from __future__ import annotations

import pytest

from acel import Session, at_most_n_times, must_precede


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        Session(mode="not_a_real_mode")


def test_default_mode_is_enforce():
    session = Session()
    assert session.mode == "enforce"


# --- call() in shadow mode ---------------------------------------------

def test_shadow_temporal_violation_does_not_raise():
    session = Session(mode="shadow")
    session.add_contract(must_precede("validate", "delete"))
    result = session.call("delete", {"id": "1"}, result={"deleted": True})
    assert result == {"deleted": True}  # call proceeded


def test_shadow_temporal_violation_is_still_recorded():
    session = Session(mode="shadow")
    session.add_contract(must_precede("validate", "delete"))
    session.call("delete", {"id": "1"}, result={"deleted": True})
    assert len(session.violations) == 1
    assert session.violations[0].kind == "temporal"
    assert len(session.evidence) == 1  # still hash-chained into evidence


def test_shadow_temporal_violation_is_added_to_trace():
    """Unlike enforce mode, a shadow-observed call still lands in the trace —
    it really happened, ACEL just didn't stop it."""
    session = Session(mode="shadow")
    session.add_contract(must_precede("validate", "delete"))
    session.call("delete", {"id": "1"}, result={"deleted": True})
    assert [row["tool"] for row in session.trace] == ["delete"]


def test_shadow_precondition_violation_does_not_block():
    session = Session(state={"authenticated": False}, mode="shadow")
    session.register_tool("read_user_data", precondition=lambda s: s.get("authenticated") is True)
    result = session.call("read_user_data", {"query": "x"}, result={"rows": []})
    assert result == {"rows": []}
    assert len(session.violations) == 1
    assert session.violations[0].kind == "precondition"


def test_shadow_cardinality_violation_still_lets_both_calls_through():
    session = Session(mode="shadow")
    session.add_contract(at_most_n_times("pay", 1))
    session.call("pay", {"amt": 1}, result={"ok": True})
    session.call("pay", {"amt": 1}, result={"ok": True})  # would halt in enforce mode
    assert len(session.trace) == 2
    assert len(session.violations) == 1  # the second call


def test_shadow_postcondition_violation_still_commits():
    """Shadow mode observes reality as-is: even a failed postcondition still
    commits the real result, since a system without ACEL would have too."""
    session = Session(state={"current_tenant": "t_9"}, mode="shadow")
    committed = {}
    session.register_tool(
        "search_database",
        postcondition=lambda s, r: r["tenant_id"] == s.get("current_tenant"),
        commit=lambda s, args, result: committed.update(result),
    )
    result = session.call("search_database", {"q": "x"}, result={"tenant_id": "t_OTHER"})
    assert result == {"tenant_id": "t_OTHER"}
    assert committed == {"tenant_id": "t_OTHER"}
    assert len(session.violations) == 1
    assert session.violations[0].kind == "postcondition"


def test_enforce_mode_unaffected_by_shadow_logic():
    """Sanity check: default (enforce) mode still halts exactly as before."""
    from acel import ContractViolation

    session = Session()  # mode="enforce" by default
    session.add_contract(must_precede("validate", "delete"))
    with pytest.raises(ContractViolation):
        session.call("delete", {"id": "1"})
    assert session.trace == []


# --- precheck()/postcheck() in shadow mode (the MCP proxy path) --------

def test_shadow_precheck_returns_violation_but_not_blocking():
    session = Session(mode="shadow")
    session.add_contract(must_precede("validate", "delete"))
    gate = session.precheck("delete", {"id": "1"})
    assert gate.violation is not None  # the fact: a rule broke
    assert gate.blocking is False  # the decision: don't stop
    assert gate.allowed is True  # middleware should forward the call


def test_enforce_precheck_returns_blocking_gate():
    session = Session()  # enforce
    session.add_contract(must_precede("validate", "delete"))
    gate = session.precheck("delete", {"id": "1"})
    assert gate.violation is not None
    assert gate.blocking is True
    assert gate.allowed is False


def test_shadow_postcheck_always_returns_none():
    session = Session(state={"current_tenant": "t_9"}, mode="shadow")
    session.register_tool(
        "search_database",
        postcondition=lambda s, r: r["tenant_id"] == s.get("current_tenant"),
    )
    gate = session.precheck("search_database", {"q": "x"})
    violation = session.postcheck("search_database", gate, {"tenant_id": "t_OTHER"})
    assert violation is None  # caller should forward the real result
    assert len(session.violations) == 1  # but it was still recorded
