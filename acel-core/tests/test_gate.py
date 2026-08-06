"""Tests for Session.precheck/postcheck — the async-safe gate used by the MCP proxy.

These mirror the semantics of Session.call but split around an await boundary:
precheck() must gate BEFORE the real tool runs, postcheck() finalizes after.
"""

from __future__ import annotations

from acel import Session, at_most_n_times, must_precede


def test_precheck_blocks_ordering_violation_without_executing():
    session = Session()
    session.add_contract(must_precede("validate", "delete"))
    gate = session.precheck("delete", {"id": "1"})
    assert gate.allowed is False
    assert gate.violation.kind == "temporal"
    assert len(session.trace) == 0  # nothing was recorded as having run


def test_precheck_allows_when_order_is_correct():
    session = Session()
    session.add_contract(must_precede("validate", "delete"))
    session.precheck("validate", {})
    gate = session.precheck("delete", {"id": "1"})
    assert gate.allowed is True


def test_postcheck_commits_state_and_records_trace():
    session = Session(state={"authenticated": False})
    session.register_tool(
        "authenticate", commit=lambda s, args, result: s.set("authenticated", result["ok"])
    )
    gate = session.precheck("authenticate", {"user": "p"})
    assert gate.allowed is True
    violation = session.postcheck("authenticate", gate, {"ok": True})
    assert violation is None
    assert session.state.get("authenticated") is True
    assert session.trace[-1]["tool"] == "authenticate"


def test_postcheck_blocks_bad_result_and_does_not_commit():
    session = Session(state={"current_tenant": "t_9"})
    committed = {"called": False}
    session.register_tool(
        "search_database",
        postcondition=lambda s, r: r["tenant_id"] == s.get("current_tenant"),
        commit=lambda s, args, result: committed.update(called=True),
    )
    gate = session.precheck("search_database", {"q": "x"})
    violation = session.postcheck("search_database", gate, {"tenant_id": "t_OTHER"})
    assert violation is not None
    assert violation.kind == "postcondition"
    assert committed["called"] is False
    assert len(session.trace) == 0


def test_full_gate_finalize_cycle_matches_call_semantics():
    """precheck + postcheck should reach the same end state as an equivalent call()."""
    a = Session()
    a.add_contract(at_most_n_times("pay", 1))
    a.call("pay", {"amt": 1}, result={"ok": True})

    b = Session()
    b.add_contract(at_most_n_times("pay", 1))
    gate = b.precheck("pay", {"amt": 1})
    b.postcheck("pay", gate, {"ok": True})

    assert a.trace == b.trace
