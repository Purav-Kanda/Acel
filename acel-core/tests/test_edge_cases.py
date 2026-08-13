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


def test_replay_prefix_does_not_trigger_end_session_checks():
    """The whole point of replay_prefix over replay: a required_before_session_end
    contract shouldn't fire just because the trace-so-far hasn't included it
    yet — more calls are still expected."""
    session = Session(halt_on_violation=False)
    session.add_contract(required_before_session_end("commit"))
    violations = session.replay_prefix([{"tool": "work"}])
    assert violations == []


def test_replay_prefix_still_advances_temporal_state():
    session = Session(halt_on_violation=False)
    session.add_contract(must_precede("validate", "delete"))
    violations = session.replay_prefix([{"tool": "delete"}])
    assert any("must_precede" in v.spec for v in violations)


def test_replay_prefix_then_precheck_sees_reconstructed_state():
    """The actual use case: replay prior history, then precheck the *next*
    call and see it correctly gated by everything that came before."""
    session = Session(halt_on_violation=False)
    session.add_contract(at_most_n_times("delete", n=1))
    session.replay_prefix([{"tool": "delete"}])  # first delete, contract now at its cap

    gate = session.precheck("delete", {})  # second delete should be blocked
    assert gate.violation is not None
    assert gate.blocking is True


def test_replay_prefix_matches_replay_state_for_same_events():
    """replay_prefix should leave the contract automata in the same place
    replay() would, minus the end-of-session check."""
    a = Session(halt_on_violation=False)
    a.add_contract(at_most_n_times("delete", n=5))
    a.replay_prefix([{"tool": "delete"}, {"tool": "delete"}])

    b = Session(halt_on_violation=False)
    b.add_contract(at_most_n_times("delete", n=5))
    b.replay([{"tool": "delete"}, {"tool": "delete"}])

    assert a.state.snapshot() == b.state.snapshot()


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
