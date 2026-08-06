"""Temporal contracts: deterministic monitors over the stream of tool names.

Each template is a small deterministic automaton. It consumes one tool name at
a time in O(1) and reports a three-valued :class:`~acel.verdict.Verdict`. The
named templates below are the developer-facing API — a user never writes raw
temporal-logic syntax; they compose these instead.

Semantics summary (``a``, ``b`` are tool names):

- ``must_precede(a, b)``            every ``b`` must be preceded by some ``a``.
- ``at_most_n_times(a, n)``         ``a`` occurs at most ``n`` times per session.
- ``at_most_total(a, field, limit)``the sum of ``args[field]`` across all calls
                                     to ``a`` must not exceed ``limit``.
- ``never_after(a, b)``             ``a`` must never occur after ``b`` has occurred.
- ``required_before_session_end(a)````a`` must occur at least once before the session ends.
- ``cannot_follow_without(a, b)``   ``a`` may not occur unless ``b`` occurred earlier
                                     (the dual of ``must_precede(b, a)``).
- ``mutually_exclusive(a, b)``      ``a`` and ``b`` must not both occur in one session.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .verdict import Verdict


class TemporalContract(ABC):
    """Base class for a single temporal monitor.

    Subclasses implement :meth:`_step` (and optionally :meth:`_finalize`).
    The base class latches ``VIOLATED`` so the verdict is monotone: once a
    contract is broken it stays broken regardless of later events.
    """

    def __init__(self, spec: str) -> None:
        self.spec = spec
        self._verdict = Verdict.UNKNOWN

    @property
    def verdict(self) -> Verdict:
        return self._verdict

    def on_event(self, tool: str, args: dict[str, Any] | None = None) -> Verdict:
        """Advance the automaton by one tool call and return the new verdict.

        ``args`` is optional and ``None`` by default — most templates only
        care about the tool *name* (ordering/cardinality rules). It exists
        for templates like :class:`CumulativeLimit` that need to read a
        numeric field out of the call's arguments (e.g. summing a payment
        amount); those templates receive ``{}`` rather than ``None`` if the
        caller omits args, so ``.get(...)`` is always safe to call on it.
        """
        if self._verdict is Verdict.VIOLATED:
            return Verdict.VIOLATED
        self._verdict = self._step(tool, args or {})
        return self._verdict

    def on_session_end(self) -> Verdict:
        """Finalize the verdict when the session closes (for co-safety rules)."""
        if self._verdict is Verdict.VIOLATED:
            return Verdict.VIOLATED
        self._verdict = self._finalize()
        return self._verdict

    def reset(self) -> None:
        """Return the monitor to its initial state (reusable across sessions)."""
        self._verdict = Verdict.UNKNOWN
        self._reset()

    # --- subclass hooks -------------------------------------------------
    @abstractmethod
    def _step(self, tool: str, args: dict[str, Any]) -> Verdict: ...

    def _finalize(self) -> Verdict:
        return self._verdict

    def _reset(self) -> None:  # pragma: no cover - default no-op
        pass

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.spec} :: {self._verdict.value}>"


class MustPrecede(TemporalContract):
    """Every occurrence of ``later`` must be preceded by at least one ``earlier``."""

    def __init__(self, earlier: str, later: str) -> None:
        super().__init__(f"must_precede({earlier}, {later})")
        self.earlier = earlier
        self.later = later
        self._seen_earlier = False

    def _step(self, tool: str, args: dict[str, Any]) -> Verdict:
        if tool == self.earlier:
            self._seen_earlier = True
            return Verdict.SATISFIED
        if tool == self.later and not self._seen_earlier:
            return Verdict.VIOLATED
        return Verdict.SATISFIED if self._seen_earlier else Verdict.UNKNOWN

    def _finalize(self) -> Verdict:
        return Verdict.SATISFIED if self._seen_earlier else Verdict.UNKNOWN

    def _reset(self) -> None:
        self._seen_earlier = False


class AtMostNTimes(TemporalContract):
    """``tool`` may be called at most ``n`` times per session."""

    def __init__(self, tool: str, n: int) -> None:
        if n < 0:
            raise ValueError("n must be non-negative")
        super().__init__(f"at_most_n_times({tool}, n={n})")
        self.tool = tool
        self.n = n
        self._count = 0

    def _step(self, tool: str, args: dict[str, Any]) -> Verdict:
        if tool == self.tool:
            self._count += 1
            if self._count > self.n:
                return Verdict.VIOLATED
        return Verdict.UNKNOWN

    def _reset(self) -> None:
        self._count = 0


class NeverAfter(TemporalContract):
    """``a`` must never occur after ``b`` has occurred."""

    def __init__(self, a: str, b: str) -> None:
        super().__init__(f"never_after({a}, {b})")
        self.a = a
        self.b = b
        self._seen_b = False

    def _step(self, tool: str, args: dict[str, Any]) -> Verdict:
        if tool == self.b:
            self._seen_b = True
        if tool == self.a and self._seen_b:
            return Verdict.VIOLATED
        return Verdict.UNKNOWN

    def _reset(self) -> None:
        self._seen_b = False


class RequiredBeforeSessionEnd(TemporalContract):
    """``tool`` must be called at least once before the session ends."""

    def __init__(self, tool: str) -> None:
        super().__init__(f"required_before_session_end({tool})")
        self.tool = tool
        self._seen = False

    def _step(self, tool: str, args: dict[str, Any]) -> Verdict:
        if tool == self.tool:
            self._seen = True
            return Verdict.SATISFIED
        return Verdict.SATISFIED if self._seen else Verdict.UNKNOWN

    def _finalize(self) -> Verdict:
        return Verdict.SATISFIED if self._seen else Verdict.VIOLATED

    def _reset(self) -> None:
        self._seen = False


class CannotFollowWithout(TemporalContract):
    """``action`` may not occur unless ``prerequisite`` occurred earlier.

    This is the dual of ``must_precede(prerequisite, action)``; it is offered as
    its own template because it reads more naturally when framed from the
    dependent action ("delete cannot happen without a prior backup").
    """

    def __init__(self, action: str, prerequisite: str) -> None:
        super().__init__(f"cannot_follow_without({action}, {prerequisite})")
        self.action = action
        self.prerequisite = prerequisite
        self._seen_prereq = False

    def _step(self, tool: str, args: dict[str, Any]) -> Verdict:
        if tool == self.prerequisite:
            self._seen_prereq = True
        if tool == self.action and not self._seen_prereq:
            return Verdict.VIOLATED
        return Verdict.SATISFIED if self._seen_prereq else Verdict.UNKNOWN

    def _finalize(self) -> Verdict:
        return Verdict.SATISFIED if self._seen_prereq else Verdict.UNKNOWN

    def _reset(self) -> None:
        self._seen_prereq = False


class MutuallyExclusive(TemporalContract):
    """``a`` and ``b`` must not both occur within a single session."""

    def __init__(self, a: str, b: str) -> None:
        super().__init__(f"mutually_exclusive({a}, {b})")
        self.a = a
        self.b = b
        self._seen_a = False
        self._seen_b = False

    def _step(self, tool: str, args: dict[str, Any]) -> Verdict:
        if tool == self.a:
            self._seen_a = True
        elif tool == self.b:
            self._seen_b = True
        if self._seen_a and self._seen_b:
            return Verdict.VIOLATED
        return Verdict.UNKNOWN

    def _reset(self) -> None:
        self._seen_a = False
        self._seen_b = False


class CumulativeLimit(TemporalContract):
    """The sum of ``args[field]`` across every call to ``tool`` must not
    exceed ``limit`` (e.g. "total refunds this session must not exceed $500").

    Unlike the other templates, this one reads the call's *arguments*, not
    just its name — it's checked before the call executes (same as every
    other temporal contract), using the amount the call is *about to* commit,
    so a call that would push the running total over the limit is blocked
    before it happens, not after.

    Fails closed: if ``field`` is missing from a matching call's arguments,
    or isn't numeric, that call is treated as a violation rather than
    silently passed through — for a contract whose whole purpose is
    capping spend, silently ignoring an unreadable amount would be the one
    genuinely dangerous failure mode.
    """

    def __init__(self, tool: str, field: str, limit: float) -> None:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        super().__init__(f"at_most_total({tool}, {field}, limit={limit})")
        self.tool = tool
        self.field = field
        self.limit = limit
        self._total: float = 0.0

    def _step(self, tool: str, args: dict[str, Any]) -> Verdict:
        if tool != self.tool:
            return Verdict.UNKNOWN if self._total == 0 else Verdict.SATISFIED

        value = args.get(self.field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return Verdict.VIOLATED  # unreadable amount — fail closed, not silently allow

        candidate = self._total + value
        if candidate > self.limit:
            return Verdict.VIOLATED
        self._total = candidate
        return Verdict.SATISFIED

    def _finalize(self) -> Verdict:
        return Verdict.SATISFIED if self._total > 0 else Verdict.UNKNOWN

    def _reset(self) -> None:
        self._total = 0.0


# --- public factory functions (the named-template DSL) -----------------

def must_precede(earlier: str, later: str) -> MustPrecede:
    return MustPrecede(earlier, later)


def at_most_n_times(tool: str, n: int) -> AtMostNTimes:
    return AtMostNTimes(tool, n)


def at_most_total(tool: str, field: str, limit: float) -> CumulativeLimit:
    return CumulativeLimit(tool, field, limit)


def never_after(a: str, b: str) -> NeverAfter:
    return NeverAfter(a, b)


def required_before_session_end(tool: str) -> RequiredBeforeSessionEnd:
    return RequiredBeforeSessionEnd(tool)


def cannot_follow_without(action: str, prerequisite: str) -> CannotFollowWithout:
    return CannotFollowWithout(action, prerequisite)


def mutually_exclusive(a: str, b: str) -> MutuallyExclusive:
    return MutuallyExclusive(a, b)
