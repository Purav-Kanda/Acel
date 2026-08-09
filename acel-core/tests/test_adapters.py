"""Tests for acel.adapters — framework-agnostic (and LangChain-specific)
helpers for wiring a Session into non-MCP agent loops."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from acel import Session, at_most_total, must_precede
from acel.adapters import guard, guard_openai_tool_call
from acel.violations import ContractViolation

sys.path.insert(0, str(Path(__file__).parent.parent / "examples"))


# --- guard() -------------------------------------------------------------


def test_guard_allows_a_call_that_satisfies_the_contract():
    session = Session()
    session.add_contract(must_precede("validate", "delete"))
    calls = []

    def real_delete(**kwargs):
        calls.append(kwargs)
        return "deleted"

    guarded = guard(session, "delete", real_delete)
    session.call("validate", {})
    assert guarded(id="r1") == "deleted"
    assert calls == [{"id": "r1"}]


def test_guard_blocks_a_call_that_violates_the_contract():
    session = Session()
    session.add_contract(must_precede("validate", "delete"))
    ran = {"called": False}

    def real_delete(**kwargs):
        ran["called"] = True
        return "deleted"

    guarded = guard(session, "delete", real_delete)
    with pytest.raises(ContractViolation):
        guarded(id="r1")
    assert ran["called"] is False  # the real function never ran


def test_guard_preserves_function_name_and_docstring():
    def my_tool(**kwargs):
        """A tool that does a thing."""
        return None

    session = Session()
    guarded = guard(session, "my_tool", my_tool)
    assert guarded.__name__ == "my_tool"
    assert guarded.__doc__ == "A tool that does a thing."


def test_guard_returns_violation_in_shadow_halt_disabled_mode():
    session = Session(halt_on_violation=False)
    session.add_contract(must_precede("validate", "delete"))
    guarded = guard(session, "delete", lambda **kw: "deleted")
    result = guarded(id="r1")
    assert result.kind == "temporal"


# --- guard_openai_tool_call() --------------------------------------------


def _tool_call(name: str, arguments) -> dict:
    return {"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}


def test_guard_openai_tool_call_parses_json_string_arguments():
    session = Session()
    received = {}

    def func(**kwargs):
        received.update(kwargs)
        return "ok"

    result = guard_openai_tool_call(session, _tool_call("do_thing", '{"a": 1, "b": "x"}'), func)
    assert result == "ok"
    assert received == {"a": 1, "b": "x"}


def test_guard_openai_tool_call_accepts_already_parsed_dict_arguments():
    session = Session()
    received = {}

    def func(**kwargs):
        received.update(kwargs)
        return "ok"

    guard_openai_tool_call(session, _tool_call("do_thing", {"a": 1}), func)
    assert received == {"a": 1}


def test_guard_openai_tool_call_blocks_on_contract_violation():
    session = Session()
    session.add_contract(must_precede("verify_customer", "issue_refund"))
    with pytest.raises(ContractViolation):
        guard_openai_tool_call(
            session, _tool_call("issue_refund", '{"amount": 10}'), lambda **kw: "refunded"
        )


def test_guard_openai_tool_call_enforces_cumulative_limit():
    session = Session()
    session.add_contract(at_most_total("issue_refund", "amount", limit=100))
    func = lambda **kw: {"refunded": kw["amount"]}  # noqa: E731

    guard_openai_tool_call(session, _tool_call("issue_refund", {"amount": 60}), func)
    with pytest.raises(ContractViolation):
        guard_openai_tool_call(session, _tool_call("issue_refund", {"amount": 60}), func)


# --- examples --------------------------------------------------------------


def test_openai_function_calling_example_runs_end_to_end(capsys):
    import openai_function_calling_example as example

    example.__name__ = "__main__"  # unused, kept for clarity of intent
    session = example.build_session()
    result = example.handle_tool_call(
        session, {"function": {"name": "verify_customer", "arguments": '{"customer_id": "c1"}'}}
    )
    assert result == {"customer_id": "c1", "verified": True}


def test_langchain_example_blocks_out_of_order_call():
    pytest.importorskip("langchain_core")
    import langchain_agent_example as example

    session = example.build_session()
    _, delete_tool = example.build_tools(session)
    with pytest.raises(ContractViolation):
        delete_tool.func(record_id="r1")


def test_langchain_example_allows_in_order_calls():
    pytest.importorskip("langchain_core")
    import langchain_agent_example as example

    session = example.build_session()
    validate_tool, delete_tool = example.build_tools(session)
    assert "validated" in validate_tool.func(record_id="r1")
    assert "deleted" in delete_tool.func(record_id="r1")


def test_langchain_guarded_tool_keeps_name_and_description():
    pytest.importorskip("langchain_core")
    import langchain_agent_example as example

    session = example.build_session()
    validate_tool, delete_tool = example.build_tools(session)
    assert validate_tool.name == "validate_record"
    assert "Validate" in validate_tool.description
