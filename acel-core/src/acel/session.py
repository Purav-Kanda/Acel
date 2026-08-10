"""The session harness that ties contracts, state, and tool calls together.

This is the MCP-independent core. In live mode (:meth:`Session.call`) it gates
each call *before execution* and halts the moment a contract is violated. In
:meth:`Session.replay` it runs a recorded trace and returns every violation it
finds, without executing anything — the basis for the ``acel replay`` CLI and
the correctness test suite.

**Concurrency.** A single ``Session`` is not meant to be shared across
multiple simultaneous *connections* — see ``acel.mcp_middleware`` for that
(one isolated ``Session`` per connection via ``session_factory``). Within
one connection, though, a client can legitimately have more than one
``tools/call`` request in flight at once (pipelined, not necessarily
awaited one at a time), and ``Session`` is safe for that: every method that
mutates shared state (``call``, ``precheck``, ``postcheck``, ``replay``,
``end_session``) is guarded by an internal ``threading.RLock``. This also
makes ``Session`` safe to call directly from multiple OS threads (e.g. a
Flask/WSGI-style app dispatching requests to a thread pool, not just an
asyncio event loop) — a real usage pattern, since ``Session.call()`` is
plain synchronous Python usable from any framework, not just the MCP
proxy. The lock only wraps each method's own synchronous body; it is never
held across an ``await`` (the real tool handler runs, unguarded, between
``precheck`` and ``postcheck`` in the MCP middleware) and it is reentrant,
so a ``commit`` callback that reads back through the same session's public
API from the same thread won't deadlock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .conditions import Check, Postcondition, Precondition, collect_checks, describe
from .events import ToolCallEvent
from .evidence import EvidenceLog, Signer
from .metrics import Metrics
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

    ``violation`` is the fact: was a rule broken. ``blocking`` is the decision:
    should execution actually be stopped. In shadow mode a violation can be
    recorded (``violation is not None``) while ``blocking`` is False — the
    call is observed and logged, never halted.
    """

    step: int
    args: dict[str, Any]
    violation: Violation | None
    blocking: bool = False

    @property
    def allowed(self) -> bool:
        return not self.blocking


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
    """A single agent session under contract enforcement.

    ``mode`` controls what a detected violation actually does:

    - ``"enforce"`` (default): a violation blocks the call — the real tool
      never runs (or its result is discarded), matching every example
      elsewhere in this codebase.
    - ``"shadow"``: a violation is still detected, recorded, and written to
      the evidence log exactly as in enforce mode — but the call is allowed
      to proceed as if ACEL weren't there. This is the recommended way to
      onboard a new set of contracts: see what they would have caught against
      real traffic before anything is actually blocked.

    Pass ``metrics=Metrics()`` (from :mod:`acel.metrics`) to have this
    session track call volume, gate latency, and violation counts (overall
    and per-contract) as it runs, renderable as Prometheus text via
    ``session.metrics.render_prometheus()``. Entirely opt-in — leave it
    ``None`` (the default) and none of that bookkeeping happens.
    """

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        *,
        halt_on_violation: bool = True,
        signer: Signer | None = None,
        mode: str = "enforce",
        redact_fields: Iterable[str] | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        if mode not in ("enforce", "shadow"):
            raise ValueError(f"mode must be 'enforce' or 'shadow', got {mode!r}")
        self.state = StateStore(state)
        self.halt_on_violation = halt_on_violation
        self.mode = mode
        self.violations: list[Violation] = []
        self.evidence = EvidenceLog(signer=signer, redact_fields=redact_fields)
        self.metrics = metrics
        self._contracts: list[TemporalContract] = []
        self._tools: dict[str, ToolSpec] = {}
        self._trace: list[dict[str, Any]] = []
        self._step = 0
        self._lock = threading.RLock()
        self._groups: dict[str, list[TemporalContract]] = {}

    # --- registration ---------------------------------------------------
    def add_contract(self, contract: TemporalContract) -> TemporalContract:
        """Register a temporal contract. Safe to call mid-session."""
        self._contracts.append(contract)
        return contract

    def add_contracts(self, contracts: Iterable[TemporalContract]) -> None:
        for contract in contracts:
            self.add_contract(contract)

    def add_contract_group(
        self, name: str, contracts: Iterable[TemporalContract]
    ) -> list[TemporalContract]:
        """Register several contracts together under one named group.

        Purely an ergonomics/organizational feature — a group is not a new
        kind of contract and doesn't change enforcement at all; every
        contract in it is registered and evaluated exactly as if you'd
        called :meth:`add_contract` on each one individually. What a group
        buys you is a name: for a server with many rules, being able to say
        "these three contracts are the refund policy" instead of tracking
        them as an unlabeled flat list — useful when reviewing
        ``session.contracts``/``session.groups``, or when a config file
        wants to define a reusable bundle once (see ``acel.config`` for the
        ``groups:`` / ``{"group": "..."}`` rules-file syntax).

        Raises ``ValueError`` if ``name`` was already used for a group on
        this session — group names must be unique per session, the same
        way a rules file's ``groups:`` keys must be.
        """
        if name in self._groups:
            raise ValueError(f"a contract group named {name!r} was already added to this session")
        materialized = list(contracts)
        self.add_contracts(materialized)
        self._groups[name] = materialized
        return materialized

    @property
    def groups(self) -> dict[str, list[TemporalContract]]:
        """All named contract groups added via :meth:`add_contract_group`,
        as ``{group_name: [contracts...]}``. Contracts added individually
        via :meth:`add_contract`/:meth:`add_contracts` (not part of any
        group) are not included here — see :attr:`contracts` for the full
        flat list regardless of grouping."""
        return {name: list(contracts) for name, contracts in self._groups.items()}

    def contracts_in_group(self, name: str) -> list[TemporalContract]:
        """The contracts in a named group. Raises ``KeyError`` if ``name``
        was never added via :meth:`add_contract_group`."""
        return list(self._groups[name])

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
        execute -> postcondition gate -> commit -> record. In ``"enforce"``
        mode (the default), a violation at any gate halts *before* the effect
        it would guard (the tool is not run, or its result is not committed).
        In ``"shadow"`` mode, violations are still recorded and written to
        evidence, but never stop the call — see the ``Session`` docstring.
        Returns the tool result on success (or in shadow mode, always).

        This method holds the session's internal lock for its entire body,
        including a synchronous ``run`` callback if one is given — unlike
        :meth:`precheck`/:meth:`postcheck`, which split the same gate logic
        around an external ``await`` specifically so the lock is never held
        across it. If your tool execution is slow, prefer the async
        precheck/postcheck split (what the MCP middleware uses) over calling
        a slow ``run`` through this method from multiple threads.
        """
        args = dict(args or {})
        with self._lock:
            self._step += 1
            step = self._step
            spec = self._tools.get(tool)
            enforcing = self.mode == "enforce"
            if self.metrics is not None:
                self.metrics.record_call()
            gate_start = time.perf_counter() if self.metrics is not None else 0.0

            # 1. Preconditions: state must permit the call.
            blocking_violation: Violation | None = None
            if spec is not None:
                for check in spec.preconditions:
                    if not check.evaluate(self.state):
                        blocking_violation = self._record(
                            "precondition", check.description, tool, step, args, None
                        )
                        break

            # 2. Temporal ordering: the call must be legal in this sequence.
            if blocking_violation is None:
                for contract in self._contracts:
                    if contract.on_event(tool, args) is Verdict.VIOLATED:
                        blocking_violation = self._record(
                            "temporal", contract.spec, tool, step, args, None
                        )
                        break

            if self.metrics is not None:
                self.metrics.record_gate_latency(time.perf_counter() - gate_start)

            if blocking_violation is not None and enforcing:
                if self.halt_on_violation:
                    raise ContractViolation(blocking_violation)
                return blocking_violation

            # 3. Execute (live) or accept a supplied result (manual/testing).
            if run is not None:
                result = run(**args)

            # 4. Postconditions: the result must be valid before we trust it.
            if spec is not None:
                for check in spec.postconditions:
                    if not check.evaluate(self.state, result):
                        post_violation = self._record(
                            "postcondition", check.description, tool, step, args, result
                        )
                        if enforcing:
                            if self.halt_on_violation:
                                raise ContractViolation(post_violation)
                            return post_violation
                        break  # shadow mode: recorded, but keep going to commit + record
                # 5. Commit the trusted result into state.
                if spec.commit is not None:
                    spec.commit(self.state, args, result)

            # 6. Record the successful (or shadow-observed) call.
            self._trace.append({"step": step, "tool": tool, "args": args, "result": result})
            return result

    # --- async/transport-facing gate (used by the MCP proxy middleware) --
    def precheck(self, tool: str, args: dict[str, Any] | None = None) -> "Gate":
        """Run the precondition + temporal gates for ``tool`` without executing it.

        Unlike :meth:`call`, this never raises and never runs the tool — it is
        meant for callers (like an async MCP middleware) that must decide
        whether to forward a call *before* awaiting the real handler, then
        finalize afterward with :meth:`postcheck` once a result exists. In
        shadow mode, a violation is still recorded but ``gate.blocking`` is
        False, so the caller should forward the call regardless.

        **Security note — preconditions are not safe for concurrency limits.**
        A precondition only *reads* ``self.state``; state isn't updated until
        :meth:`postcheck` calls ``spec.commit`` afterward, which happens once
        the (possibly slow) real tool has finished running. If a client has
        two calls to the *same tool* in flight on the same connection — which
        MCP explicitly allows and this session supports — both can call
        ``precheck`` and read the same pre-commit state before either one's
        ``postcheck`` commits, so a precondition like
        ``lambda s: s["balance"] >= amount`` can pass twice against a balance
        that should only cover one of the two calls (a TOCTOU race). Temporal
        contracts (``at_most_total``, ``rate_limit``, ``at_most_n_times``) do
        **not** have this problem — their counters are mutated synchronously,
        under the lock, inside this same ``precheck`` call — so **any
        spend/quantity/rate cap should be expressed as a temporal contract,
        not as a state-read precondition**, if it needs to hold under
        concurrent/pipelined calls.
        """
        args = dict(args or {})
        with self._lock:
            self._step += 1
            step = self._step
            spec = self._tools.get(tool)
            enforcing = self.mode == "enforce"
            if self.metrics is not None:
                self.metrics.record_call()
            gate_start = time.perf_counter() if self.metrics is not None else 0.0

            if spec is not None:
                for check in spec.preconditions:
                    if not check.evaluate(self.state):
                        v = self._record("precondition", check.description, tool, step, args, None)
                        if self.metrics is not None:
                            self.metrics.record_gate_latency(time.perf_counter() - gate_start)
                        return Gate(step, args, v, blocking=enforcing)

            for contract in self._contracts:
                if contract.on_event(tool, args) is Verdict.VIOLATED:
                    v = self._record("temporal", contract.spec, tool, step, args, None)
                    if self.metrics is not None:
                        self.metrics.record_gate_latency(time.perf_counter() - gate_start)
                    return Gate(step, args, v, blocking=enforcing)

            if self.metrics is not None:
                self.metrics.record_gate_latency(time.perf_counter() - gate_start)
            return Gate(step, args, None, blocking=False)

    def postcheck(self, tool: str, gate: "Gate", result: Any) -> Violation | None:
        """Finalize a call that passed :meth:`precheck`, given its real result.

        Validates postconditions, commits the trusted result into state, and
        records the call in the trace. In enforce mode, returns the Violation
        if a postcondition failed (the result must then be treated as
        untrusted — do not let it reach the agent). In shadow mode, a failed
        postcondition is still recorded, but this always returns None so the
        caller forwards the real result regardless.
        """
        with self._lock:
            spec = self._tools.get(tool)
            enforcing = self.mode == "enforce"
            if spec is not None:
                for check in spec.postconditions:
                    if not check.evaluate(self.state, result):
                        violation = self._record(
                            "postcondition", check.description, tool, gate.step, gate.args, result
                        )
                        if enforcing:
                            return violation
                        break  # shadow mode: recorded, but keep going to commit + record
                if spec.commit is not None:
                    spec.commit(self.state, gate.args, result)

            self._trace.append({"step": gate.step, "tool": tool, "args": gate.args, "result": result})
            return None

    def end_session(self) -> list[Violation]:
        """Finalize co-safety contracts (e.g. ``required_before_session_end``).

        Never raises: at session end there is nothing left to halt, so any
        outstanding violation is recorded and returned for the caller to act on.
        """
        with self._lock:
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
        with self._lock:
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
                    if contract.on_event(event.tool, args) is Verdict.VIOLATED and not already:
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
        if self.metrics is not None:
            self.metrics.record_violation(kind, spec)
        return violation

    def _already_reported(self, spec: str) -> bool:
        return any(v.spec == spec for v in self.violations)
