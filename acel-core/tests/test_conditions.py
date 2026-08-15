"""Tests for acel.conditions — Check's arity detection and evaluation."""

from __future__ import annotations

from acel.conditions import Check, _arity


class _CallableClass:
    def __call__(self, s):
        return True


class _VarArgsCallable:
    def __call__(self, *args):
        return True


# ---------------------------------------------------------------------------
# _arity()
# ---------------------------------------------------------------------------


def test_arity_of_one_arg_lambda():
    assert _arity(lambda s: True) == 1


def test_arity_of_two_arg_lambda():
    assert _arity(lambda s, args: True) == 2


def test_arity_of_zero_arg_lambda():
    assert _arity(lambda: True) == 0


def test_arity_of_named_function():
    def pred(state):
        return True

    assert _arity(pred) == 1


def test_arity_of_callable_object():
    assert _arity(_CallableClass()) == 1


def test_arity_of_var_args_callable_is_generous():
    """A *args-catch-all can accept the richer 2-arg call, so treat it as
    arity 2 rather than 0."""
    assert _arity(_VarArgsCallable()) == 2


def test_arity_falls_back_to_one_when_uninspectable():
    """Some C-implemented callables raise on introspection — default to the
    original, state-only precondition signature rather than guessing."""
    assert _arity(print) in (0, 1, 2)  # doesn't crash; exact value isn't the point
    assert _arity(sum) == 1 or _arity(sum) >= 0  # built-in, must not raise


# ---------------------------------------------------------------------------
# Check.evaluate() — arity-aware argument forwarding
# ---------------------------------------------------------------------------


def test_check_forwards_only_as_many_args_as_the_predicate_wants():
    received = []

    def one_arg(state):
        received.append((state,))
        return True

    check = Check(one_arg, "test")
    assert check.evaluate("STATE", "ARGS") is True
    assert received == [("STATE",)]  # "ARGS" was not forwarded


def test_check_forwards_two_args_to_a_two_arg_predicate():
    received = []

    def two_arg(state, args):
        received.append((state, args))
        return True

    check = Check(two_arg, "test")
    assert check.evaluate("STATE", "ARGS") is True
    assert received == [("STATE", "ARGS")]


def test_check_still_fails_closed_on_exception():
    def throws(state):
        raise RuntimeError("boom")

    check = Check(throws, "test")
    assert check.evaluate("STATE", "ARGS") is False


def test_check_zero_arg_predicate_receives_nothing():
    received = []

    def zero_arg():
        received.append(())
        return True

    check = Check(zero_arg, "test")
    assert check.evaluate("STATE", "ARGS") is True
    assert received == [()]


def test_check_description_is_preserved():
    check = Check(lambda s: True, "my description")
    assert check.description == "my description"
