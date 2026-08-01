"""Hoare-style pre/postcondition checks and the decorators that attach them.

A *precondition* gates a tool call: it reads the session state and must return
truthy for the call to be allowed. A *postcondition* validates the tool's
result before it is committed to state. This mirrors ToolGate's contract model,
expressed in plain Python so a developer never touches formal-logic syntax.
"""

from __future__ import annotations

import inspect
import textwrap
from dataclasses import dataclass
from typing import Any, Callable

from .state import StateStore

Precondition = Callable[[StateStore], bool]
Postcondition = Callable[[StateStore, Any], bool]


@dataclass(frozen=True, slots=True)
class Check:
    """A single predicate plus a human-readable description (for evidence)."""

    fn: Callable[..., bool]
    description: str

    def evaluate(self, *args: Any) -> bool:
        """Run the predicate, failing closed if it raises.

        A predicate that throws is treated as a failed check rather than
        crashing the monitor — soundness over convenience.
        """
        try:
            return bool(self.fn(*args))
        except Exception:
            return False


def describe(fn: Callable[..., Any]) -> str:
    """Best-effort one-line description of a predicate for evidence output."""
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return getattr(fn, "__name__", repr(fn))
    src = textwrap.dedent(src).strip().replace("\n", " ")
    # Trim a decorator/wrapper prefix if we captured one.
    for marker in ("lambda", "def "):
        idx = src.find(marker)
        if idx != -1:
            src = src[idx:]
            break
    return src[:160]


_PRE_ATTR = "_acel_preconditions"
_POST_ATTR = "_acel_postconditions"


def _attach(func: Callable[..., Any], attr: str, check: Check) -> None:
    checks: list[Check] = list(getattr(func, attr, []))
    checks.append(check)
    setattr(func, attr, checks)


def precondition(pred: Precondition, *, description: str | None = None):
    """Decorator: require ``pred(state)`` to hold before this tool may run."""

    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        _attach(func, _PRE_ATTR, Check(pred, description or describe(pred)))
        return func

    return deco


def postcondition(pred: Postcondition, *, description: str | None = None):
    """Decorator: require ``pred(state, result)`` to hold after this tool runs."""

    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        _attach(func, _POST_ATTR, Check(pred, description or describe(pred)))
        return func

    return deco


def collect_checks(func: Callable[..., Any]) -> tuple[list[Check], list[Check]]:
    """Return ``(preconditions, postconditions)`` attached to ``func``."""
    return list(getattr(func, _PRE_ATTR, [])), list(getattr(func, _POST_ATTR, []))
