"""Edge cases and the replay (offline analysis) path."""

from __future__ import annotations

from acel import (
    Session,
    ToolCallEvent,
    at_most_n_times,
    must_precede,
    mutually_exclusive,
    required_before_session_end,
)


def test_empty_trace_has_no_violations():
    session = Session()
    session.add_contract(must_precede("validate", "delete"))
    assert session.replay([]) == []


def test_contract_registered_mid_session():
    """A contract added after some calls only governs calls from that point on."""
    session = Session()
    session.call("delete_record", {"id": "1"})  # no contract yet -> allowed
    session.add_contract(at_most_n_times("delete_record", 1))
    session.call("delete_record", {"id": "2"})  # first delete SEEN by the contract
    # A second observed delete now violates.
    try:
        session.call("delete_record", {"id": "3"})
        raised = False
    except Exception:
        raised = True
    assert raised is True


def test_multiple_simultaneous_contracts_all_enforced():
    session = Session(halt_on_violation=False)
    session.add_contract(must_precede("validate", "delete"))
    session.add_contract(at_most_n_times("delete", 1))
    session.add_contract(mutually_exclusive("prod", "test"))

    events = [
        {"tool": "delete", "args": {"id": "1"}},   # must_precede violation
    ]
    violations = session.replay(events)
    specs = {v.spec for v in violations}
    assert any("must_precede" in s for s in specs)


def test_replay_finds_all_distinct_violations():
    session = Session(halt_on_violation=False)
    session.add_contract(must_precede("validate", "delete"))
    session.add_contract(required_before_session_end("commit"))

    events = [
        ToolCallEvent(tool="delete", args={"id": "1"}),  # ordering violation
        ToolCallEvent(tool="work"),
    ]  # 'commit' never happens -> session-end violation
    violations = session.replay(events)
    kinds = [(v.kind, v.spec) for v in violations]
    assert any("must_precede" in s for _, s in kinds)
    assert any("required_before_session_end" in s for _, s in kinds)


def test_replay_does_not_double_report_latched_contract():
    """Once violated, a contract should be reported once, not on every later event."""
    session = Session(halt_on_violation=False)
    session.add_contract(must_precede("validate", "delete"))
    events = [
        {"tool": "delete"},
        {"tool": "delete"},
        {"tool": "delete"},
    ]
    violations = session.replay(events)
    must_precede_hits = [v for v in violations if "must_precede" in v.spec]
    assert len(must_precede_hits) == 1


def test_throwing_predicate_fails_closed():
    """A precondition that raises is treated as a failed check (sound)."""
    session = Session(halt_on_violation=False)

    def bad_predicate(state):
        raise RuntimeError("boom")

    session.register_tool("risky", precondition=bad_predicate)
    outcome = session.call("risky", {})
    assert outcome.kind == "precondition"


def test_from_record_roundtrip():
    ev = ToolCallEvent.from_record({"tool": "x", "args": {"a": 1}, "result": {"ok": True}})
    assert ev.tool == "x"
    assert ev.args == {"a": 1}
    assert ev.result == {"ok": True}
