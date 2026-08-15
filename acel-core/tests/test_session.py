"""Tests for the Session harness: gating, commit, halting, pre/postconditions."""

from __future__ import annotations

import pytest

from acel import (
    ContractViolation,
    Session,
    at_most_n_times,
    must_precede,
    postcondition,
    precondition,
)


def test_precondition_blocks_before_execution():
    """A failed precondition must halt BEFORE the tool runs."""
    session = Session(state={"authenticated": False})
    session.register_tool("read_user_data", precondition=lambda s: s.get("authenticated") is True)

    ran = {"called": False}

    def do_read(**_):
        ran["called"] = True
        return {"rows": []}

    with pytest.raises(ContractViolation) as exc:
        session.call("read_user_data", {"query": "x"}, run=do_read)

    assert ran["called"] is False  # tool never executed
    assert exc.value.violation.kind == "precondition"


def test_precondition_passes_when_state_ok():
    session = Session(state={"authenticated": True})
    session.register_tool("read_user_data", precondition=lambda s: s.get("authenticated") is True)
    result = session.call("read_user_data", {"query": "x"}, result={"rows": [1]})
    assert result == {"rows": [1]}


def test_commit_updates_state_and_unlocks_next_call():
    session = Session(state={"authenticated": False})
    session.register_tool(
        "authenticate",
        commit=lambda s, args, result: s.set("authenticated", result["ok"]),
    )
    session.register_tool("read_user_data", precondition=lambda s: s.get("authenticated") is True)

    session.call("authenticate", {"user": "p"}, result={"ok": True})
    # Now the previously-blocked call should succeed.
    assert session.call("read_user_data", {"query": "x"}, result={"rows": []}) == {"rows": []}


def test_postcondition_blocks_bad_result():
    session = Session(state={"current_tenant": "t_9"})
    session.register_tool(
        "search_database",
        postcondition=lambda s, r: r["tenant_id"] == s.get("current_tenant"),
    )
    with pytest.raises(ContractViolation) as exc:
        session.call("search_database", {"q": "x"}, result={"tenant_id": "t_OTHER"})
    assert exc.value.violation.kind == "postcondition"


def test_temporal_gate_halts_out_of_order_call():
    session = Session()
    session.add_contract(must_precede("validate_record", "delete_record"))
    with pytest.raises(ContractViolation) as exc:
        session.call("delete_record", {"id": "r_42"})
    assert exc.value.violation.kind == "temporal"
    assert "must_precede" in exc.value.violation.spec


def test_temporal_gate_allows_correct_order():
    session = Session()
    session.add_contract(must_precede("validate_record", "delete_record"))
    session.call("validate_record", {"id": "r_42"})
    session.call("delete_record", {"id": "r_42"})
    assert [row["tool"] for row in session.trace] == ["validate_record", "delete_record"]


def test_cardinality_gate_halts_second_irreversible_call():
    session = Session()
    session.add_contract(at_most_n_times("send_payment", 1))
    session.call("send_payment", {"amt": 10})
    with pytest.raises(ContractViolation):
        session.call("send_payment", {"amt": 10})


def test_evidence_carries_trace_and_state():
    session = Session(state={"authenticated": True})
    session.add_contract(must_precede("validate_record", "delete_record"))
    session.call("authenticate", {}, result={"ok": True})
    with pytest.raises(ContractViolation) as exc:
        session.call("delete_record", {"id": "r_42"})
    v = exc.value.violation
    assert v.step == 2
    assert v.tool == "delete_record"
    assert v.state_snapshot == {"authenticated": True}
    assert v.trace[0]["tool"] == "authenticate"  # prior calls captured


def test_decorator_registration_via_register():
    session = Session(state={"authenticated": True})

    @precondition(lambda s: s.get("authenticated") is True)
    @postcondition(lambda s, r: "rows" in r)
    def read_user_data(query):
        return {"rows": []}

    session.register(read_user_data)
    assert session.call("read_user_data", {"query": "x"}, result={"rows": []}) == {"rows": []}


def test_non_halting_mode_returns_violation_object():
    session = Session(halt_on_violation=False)
    session.add_contract(must_precede("validate", "delete"))
    outcome = session.call("delete", {"id": "1"})
    assert outcome.kind == "temporal"
    assert len(session.violations) == 1


# ---------------------------------------------------------------------------
# Argument-aware preconditions: lambda s, args: ... in addition to lambda s: ...
# ---------------------------------------------------------------------------


def test_precondition_can_inspect_the_calls_own_arguments():
    """The flagship case: block a specific dangerous command outright,
    something no amount of session *state* alone could express."""
    session = Session()
    session.register_tool(
        "run_shell",
        precondition=lambda s, args: "rm -rf" not in args.get("command", ""),
    )

    ran = {"called": False}

    def do_run(**_):
        ran["called"] = True
        return {"ok": True}

    with pytest.raises(ContractViolation):
        session.call("run_shell", {"command": "rm -rf /"}, run=do_run)
    assert ran["called"] is False


def test_precondition_with_args_passes_for_a_safe_call():
    session = Session()
    session.register_tool(
        "run_shell",
        precondition=lambda s, args: "rm -rf" not in args.get("command", ""),
    )
    result = session.call("run_shell", {"command": "echo hi"}, result={"ok": True})
    assert result == {"ok": True}


def test_precondition_with_args_can_also_read_state():
    """Confirms both parameters are live, not just args — a 2-argument
    precondition can combine session state with the current call's args."""
    session = Session(state={"admin": False})
    session.register_tool(
        "run_shell",
        precondition=lambda s, args: s.get("admin") is True or "sudo" not in args.get("command", ""),
    )

    with pytest.raises(ContractViolation):
        session.call("run_shell", {"command": "sudo rm file"}, run=lambda **_: None)

    session.state.set("admin", True)
    result = session.call("run_shell", {"command": "sudo rm file"}, result={"ok": True})
    assert result == {"ok": True}


def test_existing_state_only_preconditions_are_unaffected():
    """Regression guard: the original 1-argument form must keep working
    identically now that 2-argument preconditions exist."""
    session = Session(state={"authenticated": False})
    session.register_tool("read_user_data", precondition=lambda s: s.get("authenticated") is True)

    with pytest.raises(ContractViolation):
        session.call("read_user_data", {"query": "x"}, run=lambda **_: None)

    session.state.set("authenticated", True)
    assert session.call("read_user_data", {"query": "x"}, result={"rows": []}) == {"rows": []}


def test_argument_aware_precondition_works_via_precheck_postcheck_split():
    """The MCP-proxy-style gate (precheck/postcheck) must forward args to
    preconditions exactly like the synchronous call() path does."""
    session = Session()
    session.register_tool(
        "run_shell",
        precondition=lambda s, args: "rm -rf" not in args.get("command", ""),
    )
    gate = session.precheck("run_shell", {"command": "rm -rf /"})
    assert gate.violation is not None
    assert gate.blocking is True


def test_argument_aware_precondition_works_via_replay():
    session = Session(halt_on_violation=False)
    session.register_tool(
        "run_shell",
        precondition=lambda s, args: "rm -rf" not in args.get("command", ""),
    )
    violations = session.replay([{"tool": "run_shell", "args": {"command": "rm -rf /"}}])
    assert len(violations) == 1
    assert violations[0].kind == "precondition"


def test_argument_aware_precondition_via_decorator():
    from acel import postcondition, precondition

    session = Session()

    @precondition(lambda s, args: args.get("amount", 0) <= 100)
    def spend(amount):
        return {"spent": amount}

    session.register(spend)
    with pytest.raises(ContractViolation):
        session.call("spend", {"amount": 500}, run=lambda **_: None)
    assert session.call("spend", {"amount": 50}, result={"spent": 50}) == {"spent": 50}
