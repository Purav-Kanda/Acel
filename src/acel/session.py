"""The session harness that ties contracts, state, and tool calls together.

This is the MCP-independent core. In live mode (:meth:`Session.call`) it gates
each call *before execution* and halts the moment a contract is violated. In
:meth:`Session.replay` it runs a recorded trace and returns every violation it
finds, without executing anything — the basis for the ``acel replay`` CLI and
the correctness test suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .conditions import Check, Postcondition, Precondition, collect_checks, describe
from .events import ToolCallEvent
from .evidence import EvidenceLog, Signer
from .state import StateStore
from .temporal import TemporalContract
from .verdict import Verdict
from .violations import ContractViolation, Violation

CommitFn = Callable[[StateStore, dict[str, Any], Any], None]


@dataclass(frozen=True, slots=True)
class Gate:
    """The outcome of :meth:`Session.precheck`.

    Carries the step index and normalized args back to the caller so a later
    call to :meth:`Session.postcheck` can finalize the same call once its real
    result is available — needed because, unlike :meth:`Session.call`, the
    check and the execution happen on either side of an ``await`` boundary.
    """

    step: int
    args: dict[str, Any]
    violation: Violation | None

    @property
    def allowed(self) -> bool:
        return self.violation is None


@dataclass(slots=True)
class ToolSpec:
    """The contract attached to a single tool: its pre/post checks and commit."""

    name: str
    preconditions: list[Check] = field(default_factory=list)
    postconditions: list[Check] = field(default_factory=list)
    commit: CommitFn | None = None


def _as_checks(value: Any, *, post: bool) -> list[Check]:
    """Normalize ``None`` / a predicate / a ``Check`` / a list into ``list[Check]``."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    checks: list[Check] = []
    for item in items:
        if isinstance(item, Check):
            checks.append(item)
        else:
            checks.append(Check(item, describe(item)))
    return checks


class Session:
    """A single agent session under contract enforcement."""

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        *,
        halt_on_violation: bool = True,
        signer: Signer | None = None,
    ) -> None:
        self.state = StateStore(state)
        self.halt_on_violation = halt_on_violation
        self.violations: list[Violation] = []
        self.evidence = EvidenceLog(signer=signer)
        self._contracts: list[TemporalContract] = []
        self._tools: dict[str, ToolSpec] = {}
        self._trace: list[dict[str, Any]] = []
        self._step = 0

    # --- registration ---------------------------------------------------
    def add_contract(self, contract: TemporalContract) -> TemporalContract:
        """Register a temporal contract. Safe to call mid-session."""
        self._contracts.append(contract)
        return contract

    def add_contracts(self, contracts: Iterable[TemporalContract]) -> None:
        for contract in contracts:
            self.add_contract(contract)

    def register_tool(
        self,
        name: str,
        *,
        precondition: Precondition | Check | list | None = None,
        postcondition: Postcondition | Check | list | None = None,
        commit: CommitFn | None = None,
    ) -> None:
        """Attach pre/postconditions and a state-commit to a tool by name."""
        self._tools[name] = ToolSpec(
            name=name,
            preconditions=_as_checks(precondition, post=False),
            postconditions=_as_checks(postcondition, post=True),
            commit=commit,
        )

    def register(
        self,
        func: Callable[..., Any],
        *,
        name: str | None = None,
        commit: CommitFn | None = None,
    ) -> Callable[..., Any]:
        """Register a function decorated with ``@precondition`` / ``@postcondition``."""
        pre, post = collect_checks(func)
        tool_name = name or func.__name__
        self._tools[tool_name] = ToolSpec(tool_name, pre, post, commit)
        return func

    # --- introspection --------------------------------------------------
    @property
    def trace(self) -> list[dict[str, Any]]:
        return list(self._trace)

    @property
    def contracts(self) -> list[TemporalContract]:
        return list(self._contracts)

    # --- live enforcement ----------------------------------------------
    def call(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        *,
        result: Any = None,
        run: Callable[..., Any] | None = None,
    ) -> Any:
        """Attempt a tool call under enforcement.

        Order of operations: precondition gate -> temporal ordering gate ->
        execute -> postcondition gate -> commit -> record. A violation at any
        gate halts *before* the effect it would guard (the tool is not run, or
        its result is not committed). Returns the tool result on success.
        """
        args = dict(args or {})
        self._step += 1
        step = self._step
        spec = self._tools.get(tool)

        # 1. Preconditions: state must permit the call.
        if spec is not None:
            for check in spec.preconditions:
                if not check.evaluate(self.state):
                    return self._fail("precondition", check.description, tool, step, args, None)

        # 2. Temporal ordering: the call must be legal in this sequence.
        for contract in self._contracts:
            if contract.on_event(tool) is Verdict.VIOLATED:
                return self._fail("temporal", contract.spec, tool, step, args, None)

        # 3. Execute (live) or accept a supplied result (manual/testing).
        if run is not None:
            result = run(**args)

        # 4. Postconditions: the result must be valid before we trust it.
        if spec is not None:
            for check in spec.postconditions:
                if not check.evaluate(self.state, result):
                    return self._fail("postcondition", check.description, tool, step, args, result)
            # 5. Commit the trusted result into state.
            if spec.commit is not None:
                spec.commit(self.state, args, result)

        # 6. Record the successful call.
        self._trace.append({"step": step, "tool": tool, "args": args, "result": result})
        return result

    # --- async/transport-facing gate (used by the MCP proxy middleware) --
    def precheck(self, tool: str, args: dict[str, Any] | None = None) -> "Gate":
        """Run the precondition + temporal gates for ``tool`` without executing it.

        Unlike :meth:`call`, this never raises and never runs the tool — it is
        meant for callers (like an async MCP middleware) that must decide
        whether to forward a call *before* awaiting the real handler, then
        finalize afterward with :meth:`postcheck` once a result exists.
        """
        args = dict(args or {})
        self._step += 1
        step = self._step
        spec = self._tools.get(tool)

        if spec is not None:
            for check in spec.preconditions:
                if not check.evaluate(self.state):
                    v = self._record("precondition", check.description, tool, step, args, None)
                    return Gate(step, args, v)

        for contract in self._contracts:
            if contract.on_event(tool) is Verdict.VIOLATED:
                v = self._record("temporal", contract.spec, tool, step, args, None)
                return Gate(step, args, v)

        return Gate(step, args, None)

    def postcheck(self, tool: str, gate: "Gate", result: Any) -> Violation | None:
        """Finalize a call that passed :meth:`precheck`, given its real result.

        Validates postconditions, commits the trusted result into state, and
        records the call in the trace. Returns the Violation if a
        postcondition failed (the result must then be treated as untrusted —
        do not let it reach the agent), else None.
        """
        spec = self._tools.get(tool)
        if spec is not None:
            for check in spec.postconditions:
                if not check.evaluate(self.state, result):
                    return self._record(
                        "postcondition", check.description, tool, gate.step, gate.args, result
                    )
            if spec.commit is not None:
                spec.commit(self.state, gate.args, result)

        self._trace.append({"step": gate.step, "tool": tool, "args": gate.args, "result": result})
        return None

    def end_session(self) -> list[Violation]:
        """Finalize co-safety contracts (e.g. ``required_before_session_end``).

        Never raises: at session end there is nothing left to halt, so any
        outstanding violation is recorded and returned for the caller to act on.
        """
        closing: list[Violation] = []
        for contract in self._contracts:
            if contract.on_session_end() is Verdict.VIOLATED and not self._already_reported(contract.spec):
                closing.append(self._record("temporal", contract.spec, "<session-end>", self._step, {}, None))
        return closing

    # --- offline analysis ----------------------------------------------
    def replay(self, events: Iterable[ToolCallEvent | dict[str, Any]]) -> list[Violation]:
        """Run a recorded trace and return *every* violation found.

        Non-halting: unlike :meth:`call`, this never raises and never executes
        tools. Used for CI-style checking of recorded traces and for the
        correctness test suite. ``halt_on_violation`` is ignored here.
        """
        found: list[Violation] = []
        for record in events:
            event = record if isinstance(record, ToolCallEvent) else ToolCallEvent.from_record(record)
            self._step += 1
            step = self._step
            spec = self._tools.get(event.tool)
            args = dict(event.args)

            # 1. Precondition gate (short-circuits the step, matching live).
            pre_failed = False
            if spec is not None:
                for check in spec.preconditions:
                    if not check.evaluate(self.state):
                        found.append(self._record("precondition", check.description, event.tool, step, args, None))
                        pre_failed = True
                        break
            if pre_failed:
                continue

            # 2. Temporal gate — record each contract's first transition to VIOLATED.
            temporal_failed = False
            for contract in self._contracts:
                already = contract.verdict is Verdict.VIOLATED
                if contract.on_event(event.tool) is Verdict.VIOLATED and not already:
                    found.append(self._record("temporal", contract.spec, event.tool, step, args, None))
                    temporal_failed = True
            if temporal_failed:
                continue

            # 3. Postcondition gate + commit.
            if spec is not None:
                post_failed = False
                for check in spec.postconditions:
                    if not check.evaluate(self.state, event.result):
                        found.append(self._record("postcondition", check.description, event.tool, step, args, event.result))
                        post_failed = True
                        break
                if post_failed:
                    continue
                if spec.commit is not None:
                    spec.commit(self.state, args, event.result)

            self._trace.append({"step": step, "tool": event.tool, "args": args, "result": event.result})

        found.extend(self.end_session())
        return found

    # --- internals ------------------------------------------------------
    def _record(
        self, kind: str, spec: str, tool: str, step: int, args: dict[str, Any], result: Any
    ) -> Violation:
        violation = Violation(
            kind=kind,
            spec=spec,
            tool=tool,
            step=step,
            trace=list(self._trace),
            state_snapshot=self.state.snapshot(),
            args=args,
            result=result,
        )
        self.violations.append(violation)
        self.evidence.record(violation)
        return violation

    def _fail(
        self, kind: str, spec: str, tool: str, step: int, args: dict[str, Any], result: Any
    ) -> Violation:
        violation = self._record(kind, spec, tool, step, args, result)
        if self.halt_on_violation:
            raise ContractViolation(violation)
        return violation

    def _already_reported(self, spec: str) -> bool:
        return any(v.spec == spec for v in self.violations)
