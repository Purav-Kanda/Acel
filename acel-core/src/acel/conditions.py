"""Hoare-style pre/postcondition checks and the decorators that attach them.

A *precondition* gates a tool call: it reads the session state (and,
optionally, the call's own arguments — see below) and must return truthy
for the call to be allowed. A *postcondition* validates the tool's result
before it is committed to state. This mirrors ToolGate's contract model,
expressed in plain Python so a developer never touches formal-logic syntax.

**Preconditions can optionally see the current call's arguments.** The
original, still fully supported form is ``lambda s: ...`` (state only) —
this is what every example elsewhere in this codebase uses, and what most
preconditions actually need ("is the session authenticated"). But a
precondition can also be written ``lambda s, args: ...`` to inspect the
*specific call* being gated, not just accumulated session state — e.g.
``lambda s, args: "rm -rf" not in args.get("command", "")`` to block a
specific dangerous shell command outright, something no amount of session
state can express. :class:`Check` detects which form a given predicate uses
by inspecting its signature (see :func:`_arity`) and calls it accordingly —
existing 1-argument preconditions keep working exactly as before, with zero
changes required.
"""

from __future__ import annotations

import inspect
import textwrap
from dataclasses import dataclass, field
from typing import Any, Callable

from .state import StateStore

Precondition = Callable[..., bool]  # (state) -> bool, or (state, args) -> bool
Postcondition = Callable[[StateStore, Any], bool]


def _arity(fn: Callable[..., Any]) -> int:
    """Best-effort count of ``fn``'s positional parameters, for deciding how
    many arguments :meth:`Check.evaluate` actually forwards to it.

    Falls back to ``1`` (the original, state-only precondition signature)
    if the callable can't be introspected — a safe default that preserves
    old behavior for anything unusual (e.g. some C-implemented callables)
    rather than guessing wrong and passing an object the function isn't
    prepared to receive.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return 1
    count = 0
    for param in sig.parameters.values():
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            count += 1
        elif param.kind is param.VAR_POSITIONAL:
            return 2  # a *args-style catch-all can accept the richer call
    return count


@dataclass(frozen=True, slots=True)
class Check:
    """A single predicate plus a human-readable description (for evidence)."""

    fn: Callable[..., bool]
    description: str
    _arity: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_arity", _arity(self.fn))

    def evaluate(self, *args: Any) -> bool:
        """Run the predicate, failing closed if it raises.

        Callers always pass every argument that *might* be relevant (e.g.
        both ``state`` and the call's ``args`` for a precondition check) —
        this method forwards only as many of them as ``fn``'s own arity
        calls for, so a 1-argument ``lambda s: ...`` and a 2-argument
        ``lambda s, args: ...`` both work correctly from the same call
        site, with no signature negotiation needed by the caller.

        A predicate that throws is treated as a failed check rather than
        crashing the monitor — soundness over convenience.
        """
        try:
            return bool(self.fn(*args[: self._arity]))
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
    """Decorator: require ``pred(state)`` — or ``pred(state, args)`` to also
    inspect the current call's arguments — to hold before this tool may run.
    See the module docstring for why/when to use the two-argument form.
    """

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
