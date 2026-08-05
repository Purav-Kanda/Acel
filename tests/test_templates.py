"""Unit tests for each temporal template's automaton, in isolation."""

from __future__ import annotations

import pytest

from acel.temporal import (
    at_most_n_times,
    at_most_total,
    cannot_follow_without,
    must_precede,
    mutually_exclusive,
    never_after,
    required_before_session_end,
)
from acel.verdict import Verdict


def run(contract, tools):
    """Feed a list of tool names and return the final streaming verdict."""
    verdict = contract.verdict
    for tool in tools:
        verdict = contract.on_event(tool)
    return verdict


def run_calls(contract, calls):
    """Feed a list of (tool, args) pairs and return the final streaming verdict."""
    verdict = contract.verdict
    for tool, args in calls:
        verdict = contract.on_event(tool, args)
    return verdict


# --- must_precede ------------------------------------------------------

def test_must_precede_ok():
    c = must_precede("validate", "delete")
    assert run(c, ["validate", "delete", "delete"]) is not Verdict.VIOLATED


def test_must_precede_violation():
    c = must_precede("validate", "delete")
    assert run(c, ["read", "delete"]) is Verdict.VIOLATED


def test_must_precede_latches_violation():
    c = must_precede("validate", "delete")
    c.on_event("delete")  # violate immediately
    assert c.on_event("validate") is Verdict.VIOLATED  # sticky


# --- at_most_n_times ---------------------------------------------------

def test_at_most_n_ok_at_limit():
    c = at_most_n_times("send_payment", 1)
    assert run(c, ["send_payment"]) is not Verdict.VIOLATED


def test_at_most_n_violation_over_limit():
    c = at_most_n_times("send_payment", 1)
    assert run(c, ["send_payment", "send_payment"]) is Verdict.VIOLATED


def test_at_most_zero_forbids():
    c = at_most_n_times("delete", 0)
    assert run(c, ["delete"]) is Verdict.VIOLATED


# --- at_most_total ------------------------------------------------------

def test_at_most_total_ok_under_limit():
    c = at_most_total("send_payment", "amount", 100)
    calls = [("send_payment", {"amount": 40}), ("send_payment", {"amount": 30})]
    assert run_calls(c, calls) is not Verdict.VIOLATED


def test_at_most_total_ok_exactly_at_limit():
    c = at_most_total("send_payment", "amount", 100)
    calls = [("send_payment", {"amount": 60}), ("send_payment", {"amount": 40})]
    assert run_calls(c, calls) is not Verdict.VIOLATED


def test_at_most_total_violation_over_limit():
    c = at_most_total("send_payment", "amount", 100)
    calls = [("send_payment", {"amount": 60}), ("send_payment", {"amount": 41})]
    assert run_calls(c, calls) is Verdict.VIOLATED


def test_at_most_total_violation_single_call_over_limit():
    c = at_most_total("send_payment", "amount", 100)
    assert run_calls(c, [("send_payment", {"amount": 150})]) is Verdict.VIOLATED


def test_at_most_total_ignores_other_tools():
    c = at_most_total("send_payment", "amount", 100)
    calls = [("other_tool", {"amount": 99999}), ("send_payment", {"amount": 10})]
    assert run_calls(c, calls) is not Verdict.VIOLATED


def test_at_most_total_fails_closed_on_missing_field():
    c = at_most_total("send_payment", "amount", 100)
    assert run_calls(c, [("send_payment", {})]) is Verdict.VIOLATED


def test_at_most_total_fails_closed_on_non_numeric_field():
    c = at_most_total("send_payment", "amount", 100)
    assert run_calls(c, [("send_payment", {"amount": "a lot"})]) is Verdict.VIOLATED


def test_at_most_total_fails_closed_on_bool_field():
    """bool is a subclass of int in Python — must not silently count as 0/1."""
    c = at_most_total("send_payment", "amount", 100)
    assert run_calls(c, [("send_payment", {"amount": True})]) is Verdict.VIOLATED


def test_at_most_total_latches_violation():
    c = at_most_total("send_payment", "amount", 100)
    c.on_event("send_payment", {"amount": 150})
    assert c.on_event("send_payment", {"amount": 1}) is Verdict.VIOLATED


def test_at_most_total_negative_limit_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        at_most_total("send_payment", "amount", -1)


def test_at_most_total_reset_clears_running_total():
    c = at_most_total("send_payment", "amount", 100)
    c.on_event("send_payment", {"amount": 90})
    c.reset()
    assert c.on_event("send_payment", {"amount": 90}) is not Verdict.VIOLATED


# --- never_after -------------------------------------------------------

def test_never_after_ok_before_marker():
    c = never_after("read", "close")
    assert run(c, ["read", "read", "close"]) is not Verdict.VIOLATED


def test_never_after_violation():
    c = never_after("read", "close")
    assert run(c, ["close", "read"]) is Verdict.VIOLATED


# --- required_before_session_end --------------------------------------

def test_required_satisfied_when_seen():
    c = required_before_session_end("commit")
    run(c, ["work", "commit"])
    assert c.on_session_end() is Verdict.SATISFIED


def test_required_violated_when_missing():
    c = required_before_session_end("commit")
    run(c, ["work", "work"])
    assert c.on_session_end() is Verdict.VIOLATED


def test_required_unknown_midstream():
    c = required_before_session_end("commit")
    assert c.on_event("work") is Verdict.UNKNOWN


# --- cannot_follow_without --------------------------------------------

def test_cannot_follow_without_ok():
    c = cannot_follow_without("delete", "backup")
    assert run(c, ["backup", "delete"]) is not Verdict.VIOLATED


def test_cannot_follow_without_violation():
    c = cannot_follow_without("delete", "backup")
    assert run(c, ["delete"]) is Verdict.VIOLATED


def test_cannot_follow_without_is_dual_of_must_precede():
    a = cannot_follow_without("delete", "backup")
    b = must_precede("backup", "delete")
    seq = ["delete", "backup", "delete"]
    assert (run(a, seq) is Verdict.VIOLATED) == (run(b, seq) is Verdict.VIOLATED)


# --- mutually_exclusive -----------------------------------------------

def test_mutually_exclusive_ok_only_one():
    c = mutually_exclusive("prod_write", "test_write")
    assert run(c, ["prod_write", "prod_write"]) is not Verdict.VIOLATED


def test_mutually_exclusive_violation():
    c = mutually_exclusive("prod_write", "test_write")
    assert run(c, ["prod_write", "test_write"]) is Verdict.VIOLATED


# --- reset -------------------------------------------------------------

def test_reset_clears_violation():
    c = must_precede("validate", "delete")
    c.on_event("delete")
    assert c.verdict is Verdict.VIOLATED
    c.reset()
    assert c.verdict is Verdict.UNKNOWN
    assert c.on_event("validate") is Verdict.SATISFIED
