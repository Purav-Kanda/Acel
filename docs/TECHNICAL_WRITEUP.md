# ACEL: Runtime Verification for AI Agent Tool Calls

**A technical write-up of design, correctness, and performance**

Author: Purav Kanda · [github.com/Purav-Kanda/Acel](https://github.com/Purav-Kanda/Acel) · `pip install acel-core`

---

## Abstract

AI agents that call real tools — deleting records, sending payments, editing files — fail in a specific, avoidable way: they don't misunderstand instructions in bulk, they occasionally issue one bad call in an otherwise-correct session. Evaluation frameworks that report an agent's average behavior across many runs cannot answer the question that matters in production: *did this specific execution, right now, violate a rule we cannot allow it to violate?*

ACEL (Agent Contract Enforcement Layer) answers that question directly. It is a runtime verification layer that sits between an agent and its tools, checks each tool call against declared contracts as the call happens, and blocks the call before it executes if a contract would be violated. This document describes the system's design, the formal grounding it borrows from, what it actually guarantees (and doesn't), and the measured evidence for its correctness and performance.

---

## 1. Problem statement

An agent's tool-calling loop is a sequence of decisions made by a language model, each translated into a concrete side-effecting call: `delete_record`, `send_payment`, `read_user_data`. Two classes of failure matter here, and they require different tools to catch:

1. **Statistical failure** — the agent behaves badly on some fraction of runs. This is what evals measure: pass rates, task-completion percentages, LLM-judged quality scores. It answers "how good is this agent on average?"
2. **Instance failure** — *this* run, right now, is about to violate a rule that must never be violated: delete before backup, double payment, reading tenant data across a boundary, an action taken before authentication succeeded. An eval score of 98% is no comfort if you are the one execution in the 2%, and no eval can tell you *in the moment* whether the call currently on the stack is that one.

ACEL is built for the second class of failure. It doesn't estimate how well an agent behaves — it checks whether the concrete trace of calls that has actually happened is still consistent with the rules, and if the next call would break that consistency, it stops the call before it executes.

This is **runtime verification**, not formal verification. It does not prove the agent is correct for all possible inputs; it monitors one finite execution and reports whether that specific trace conforms to the specification, extending the check by one event with each call.

---

## 2. Design overview

ACEL has three layers, each independently useful:

1. **Temporal contracts** — deterministic automata that monitor the *order* of tool calls across a session (e.g. "validate before you delete").
2. **Pre/postconditions over symbolic state** — Hoare-style guards that read and write a small typed state store, so a call can be gated on facts the agent has *actually established* (e.g. "reads are only allowed once authentication has actually returned `ok: true`"), not facts the agent merely claims.
3. **Evidence** — every violation produces a tamper-evident, hash-chained record, so a rejected call leaves a durable, checkable trail.

A `Session` is the runtime object that owns all three: it holds the active contracts, the state store, and the evidence log, and exposes `call()` (direct library use) and `precheck()`/`postcheck()` (used by the MCP proxy, described in §5) as the two integration points.

---

## 3. Temporal contracts: deterministic finite-trace monitoring

### 3.1 Formal grounding

Ordering rules over an event stream are naturally expressed in linear temporal logic (LTL), but classical LTL is defined over infinite traces, and an agent session is finite and still running — the "trace so far" is a prefix of an unknown-length execution. ACEL's temporal layer is grounded in **LTL3**, the three-valued semantics for monitoring LTL properties over finite trace prefixes described by Bauer, Leucker, and Schallhart ("Runtime Verification for LTL and TLTL," *ACM TOSEM*, 2011). Rather than a two-valued true/false, each monitor reports one of three verdicts at every step:

- `SATISFIED` — every finite extension of the current trace still satisfies the property, given what's been observed.
- `VIOLATED` — no extension can satisfy it; the property is permanently broken (this verdict is **monotone**: once VIOLATED, ACEL latches it there for the rest of the session, since a violated safety property cannot become un-violated).
- `UNKNOWN` — not yet decidable from the trace so far; more events could go either way.

This three-valued model is exactly the right fit for the agent-monitoring setting: most of ACEL's rules are *safety properties* ("a bad prefix, once it happens, can never be fixed"), which is the class LTL3 monitoring was designed for, and the `UNKNOWN` verdict is what lets ACEL say "no violation detected yet" without falsely claiming the rule is proven satisfied for a session still in progress.

### 3.2 Seven named templates, not raw temporal logic

Nobody writing agent tools wants to write LTL formulas. ACEL exposes seven named templates — `must_precede`, `at_most_n_times`, `at_most_total`, `never_after`, `required_before_session_end`, `cannot_follow_without`, `mutually_exclusive` — each a small hand-written deterministic automaton implementing one common ordering pattern. Six of the seven only ever look at the tool *name*; `at_most_total` is the exception — it sums a numeric field out of the call's arguments (e.g. a payment amount) across the session, for rules like "total refunds this session must not exceed $500," and fails closed (treats a missing or non-numeric field as a violation) rather than silently letting an unreadable amount through. A user composes these declaratively:

```python
session.add_contract(must_precede("validate_record", "delete_record"))
session.add_contract(at_most_n_times("send_payment", n=1))
```

Internally, each template is a subclass of `TemporalContract` implementing a single `_step(tool_name) -> Verdict` method. The base class handles verdict latching (`on_event` short-circuits to `VIOLATED` once broken) and session-end finalization (`on_session_end`, for templates like `required_before_session_end` whose truth isn't decidable until the session actually closes — a co-safety property). Adding a seventh template is a ~15-line subclass, not a change to a shared evaluator; there is no general LTL parser or formula-string DSL to maintain, which was a deliberate scope cut for a template set that covers the overwhelming majority of real agent ordering rules (see §7 for what this trades away).

### 3.3 Complexity

Each contract advances by exactly one comparison per tool call — `_step` does O(1) work regardless of how many events have preceded it, because every template's state is a small fixed set of booleans/counters (`_seen_earlier`, `_count`, `_seen_a`/`_seen_b`), never a stored copy of the trace. A session with *k* active contracts costs O(k) per tool call, independent of session length. This is what makes the measured latency numbers in §6 flat with respect to trace length and linear only in the number of concurrently active contracts.

---

## 4. Pre/postconditions and symbolic state

Temporal contracts catch *ordering*. A second, orthogonal class of rule needs *facts*: "only allow this read if the caller is actually authenticated," where "actually authenticated" must mean the result of a real, prior `authenticate` call — not a claim in the current call's arguments, which an agent (or a compromised/hallucinating one) could simply assert.

ACEL's `StateStore` is a typed key-value store, scoped to one session, that mirrors the "trusted world information" model described in the ToolGate paper (Peigné et al., "Prompt Injection 2.0," 2025) — the world model only evolves through explicitly committed tool results, never through unverified agent claims:

```python
session.register_tool(
    "read_user_data",
    precondition=lambda s: s.get("authenticated") is True,
)
session.register_tool(
    "authenticate",
    commit=lambda s, args, result: s.set("authenticated", result["ok"]),
)
```

A `precondition` reads state and must hold before the call runs; a `commit` writes state *only after* the real tool result comes back, and only from that result — never from the agent's stated arguments. This closes a real failure mode: an agent that calls `read_user_data` claiming `{"authenticated": true}` in its arguments gains nothing, because the precondition never looks at the arguments — it looks at what the state store actually recorded from a prior tool's real return value.

`postcondition` is the dual: a check over `(state, result)` after the call executes but before its result is returned to the agent, for rules like "the tenant ID in the result must match the tenant the session was scoped to."

---

## 5. Enforcement point: direct calls and the live MCP proxy

ACEL supports two integration surfaces, both funneling through the same `Session`:

**Direct (`Session.call`)** — for any agent loop you control yourself, in Python, regardless of which model is driving it. You call `session.call(tool_name, args, result=...)` and a `ContractViolation` is raised (carrying the specific `Violation`: kind, spec, offending step, full trace, state snapshot) if the call would break a contract.

**Live MCP proxy (`ACELMiddleware`)** — for tools exposed through a real MCP server. This wires directly into the official MCP Python SDK's `ServerMiddleware` hook. Every `tools/call` request passes through `ACELMiddleware.__call__` *before* the server's real tool handler runs:

```python
gate = self.session.precheck(tool, arguments)
if not gate.allowed:
    return _halt_result(gate.violation)          # real handler never invoked
result = await call_next(ctx)                      # only reached if the gate passed
violation = self.session.postcheck(tool, gate, _extract_result(result))
```

This is the load-bearing design property: on a blocked call, `call_next` — the line that would dispatch to the real tool implementation — is never reached. A blocked `delete_record` call has zero side effects, not "a side effect that gets rolled back." Because enforcement happens at the MCP server, it is client-agnostic: any MCP client talking to that server — Claude Desktop, Claude Code, Cowork, or (as of September 2025) ChatGPT via its Apps & Connectors developer mode — is gated identically, since the gate is a property of the server, not of which model or product is making the request.

One real engineering issue surfaced building this proxy: the MCP SDK does not populate `CallToolResult.structured_content` unless a tool explicitly opts into typed `structured_output`; most tools instead get their return value serialized into a JSON text content block. An early version of `_extract_result` only checked `structured_content`, found it `None` for ordinary tools, and silently fed an empty dict to postconditions and commits — meaning `session.state["authenticated"]` never actually flipped to `True` even though the call had visibly succeeded. The fix (`_first_text_block` + a `json.loads` fallback with a safe default) is a small function, but the bug is a good illustration of why this needs a live SDK-backed test, not just an in-process mock: a mocked transport would have handed back exactly the shape the code expected to see.

---

## 6. Evidence: tamper-evident hash-chained violation records

On every violation, ACEL emits an `EvidenceBundle`: the violation record, a snapshot of state at the moment it broke, and a `bundle_hash = sha256(prev_hash + trace_hash)` linking it to the previous bundle — a minimal Merkle-style hash chain, no blockchain or consensus involved. Any retroactive edit to any field of any historical bundle changes that bundle's recomputed `trace_hash`, which breaks every `bundle_hash` after it in the chain; `EvidenceLog.verify_bundles` recomputes the whole chain from a list of bundle dicts (e.g. loaded back from disk) and returns `False` on any tampering, anywhere in the history. Payloads are canonicalized (`sort_keys=True`, fixed separators) before hashing so the same violation always hashes identically regardless of dict ordering.

Ed25519 signing is layered on top as a strictly optional extra (`ed25519_signer()`, requires the `cryptography` package): the hash chain's tamper-evidence holds with the standard library alone; signing adds non-repudiation (a specific keypair produced this bundle) on top of that, for anyone who needs it.

---

## 7. Correctness and performance: measured, not asserted

**Correctness.** A labeled dataset of 59 synthetic tool-call traces spans all seven temporal templates: valid sequences, violating sequences, and edge cases (empty traces, multiple simultaneous contracts, boundary counts for `at_most_n_times`, missing/non-numeric fields for `at_most_total`). Measured result: **100% precision and 100% recall** (`benchmarks/correctness.py`, wired into CI as `tests/test_correctness_suite.py`). This number is expected, not impressive on its own — the monitor is a deterministic automaton over an explicit trace, not a statistical classifier, so a correctly implemented automaton *should* score 100% on a labeled set of well-formed cases. The suite's actual value is as a regression gate: any future change to a template's `_step` logic that breaks a case fails CI immediately, and the 100% is what "not broken" looks like on this dataset.

**Performance.** `benchmarks/latency.py` measures added p95 latency per tool call, 20,000 iterations, discarding a 1,000-call warmup, on the reference dev machine: **~0.005ms at 1 active contract**, **~0.04ms at 50 concurrently active contracts** — both well under a <5ms target for anything sitting in a live request path. This scaling is a direct consequence of the O(1)-per-contract design in §3.3: cost is linear in the number of *active* contracts, flat with respect to how long the session has been running.

**Shadow mode.** Both of the above are correctness/perf claims about the monitor in isolation. The harder problem in practice is rolling a new contract set onto real traffic without an untested rule blocking something legitimate on day one. `Session(mode="shadow")` runs the exact same detection logic — same evidence log, same hash chain — but never blocks: `Gate.blocking` is forced `False` regardless of the underlying violation, so postcondition failures fall through to `commit`/trace-append instead of halting. `Session.replay()`, by contrast, is a retrospective CI-gate tool ("would this recorded trace have been blocked") and intentionally ignores `mode` — it always reports every violation, since a replay isn't a live session that needs a rollout safety net.

---

## 8. What this doesn't do (scope, honestly stated)

- **It doesn't detect semantic wrongness.** ACEL doesn't know that a `send_payment` amount is unreasonable, or that a generated SQL query is subtly wrong — it knows only what you've encoded as a contract or a state precondition. It's a specification-conformance checker, not a general correctness oracle.
- **It doesn't reason about arbitrary LTL formulas.** The seven templates cover the common ordering and cumulative-limit patterns; a rule that doesn't fit one of them (or a conjunction of them) isn't expressible without writing a new `TemporalContract` subclass. This is a deliberate scope cut, not an oversight — a general temporal-logic parser was cut early to ship a correct, well-tested core in the time available, and the templates were chosen because they cover the overwhelming majority of real "X before Y" / "at most N" / "at most $N total" / "never both" agent rules.
- **It only guards tools it fronts.** In MCP-proxy mode, ACEL gates the tools exposed through *your* server. It cannot wrap a first-party connector run on someone else's infrastructure (e.g. a hosted connector's internals) — only tools you actually expose and route through the middleware.
- **The state store trusts its own commits.** If a `commit` lambda is written to pull from unverified input rather than the verified tool result, the "trusted world" guarantee is only as good as that lambda. ACEL enforces the *mechanism* (state only changes through explicit commits, never through the current call's raw arguments); it can't stop a user from writing a careless commit function.

---

## 9. Summary

ACEL is a small, fully-tested (167 passing tests, including a live end-to-end run against the real MCP SDK and a real subprocess) runtime verification layer with three independently useful pieces: O(1)-per-event temporal automata grounded in LTL3's three-valued finite-trace semantics, Hoare-style pre/postconditions over a trusted symbolic state store, and a tamper-evident hash-chained evidence log — wired into a live MCP proxy that halts a violating call before it ever reaches the real tool. It is MIT-licensed, self-hosted, and published on PyPI as `acel-core`.

### References

- Bauer, A., Leucker, M., & Schallhart, C. (2011). *Runtime Verification for LTL and TLTL*. ACM Transactions on Software Engineering and Methodology (TOSEM), 20(4).
- Peigné et al. (2025). *ToolGate: Prompt Injection 2.0* — the "trusted world information" model that `StateStore` mirrors.
- Model Context Protocol specification and official Python SDK — [modelcontextprotocol.io](https://modelcontextprotocol.io)
