# ACEL — Agent Contract Enforcement Layer (core)

**Runtime verification for AI agent tool calls.** Declare temporal ordering
contracts and Hoare-style pre/postconditions in plain Python, and have them
enforced live against the stream of tool calls an agent makes — halting the
agent the moment a rule is broken.

> `acel-core` ships the transport-independent monitor (Phase 1) plus a live
> MCP proxy (Phase 2): the same contracts enforced against a real MCP server,
> via the official MCP Python SDK's request-middleware pipeline.

This is **runtime verification** — checking each concrete execution against a
specification as it happens. It does not prove the agent correct in general; it
guarantees that *this* run did not violate the rules you declared.

## Why

Statistical agent-eval tools answer "how often does this agent behave well, on
average?" ACEL answers the production question they can't: "did *this* execution
just violate a rule we cannot allow to be violated?" — before the bad tool call
lands.

## Install

```bash
pip install -e .
```

## Quickstart

```python
from acel import Session, must_precede, at_most_n_times

session = Session(state={"authenticated": False})

# Temporal ordering rules (no logic syntax required):
session.add_contract(must_precede("validate_record", "delete_record"))
session.add_contract(at_most_n_times("send_payment", n=1))

# State-based gate: reads only allowed once authenticated.
session.register_tool(
    "read_user_data",
    precondition=lambda s: s.get("authenticated") is True,
)
# Authentication commits trusted info into session state.
session.register_tool(
    "authenticate",
    commit=lambda s, args, result: s.set("authenticated", result["ok"]),
)

session.call("authenticate", {"user": "p"}, result={"ok": True})
session.call("read_user_data", {"query": "SELECT ..."}, result={"rows": []})

# This halts: delete before validate.
session.call("delete_record", {"id": "r_42"})   # raises ContractViolation
```

On violation, a `ContractViolation` is raised carrying a `Violation` record:

```python
from acel import ContractViolation

try:
    session.call("delete_record", {"id": "r_42"})
except ContractViolation as exc:
    v = exc.violation
    print(v.kind)            # "temporal"
    print(v.spec)            # "must_precede(validate_record, delete_record)"
    print(v.step)            # index of the offending call
    print(v.trace)           # every call up to the violation
    print(v.state_snapshot)  # symbolic state at the moment it broke
```

## The six temporal templates

| Template | Meaning |
| --- | --- |
| `must_precede(a, b)` | every `b` must be preceded by some `a` |
| `at_most_n_times(a, n)` | `a` occurs at most `n` times per session |
| `never_after(a, b)` | `a` must never occur after `b` |
| `required_before_session_end(a)` | `a` must occur at least once before the session ends |
| `cannot_follow_without(a, b)` | `a` may not occur unless `b` occurred earlier |
| `mutually_exclusive(a, b)` | `a` and `b` must not both occur in one session |

Each template is a deterministic automaton advanced in O(1) per tool call, with
a three-valued verdict (`SATISFIED` / `VIOLATED` / `UNKNOWN`) over the finite
trace.

## Pre/postconditions with decorators

```python
from acel import Session, precondition, postcondition

@precondition(lambda s: s.get("authenticated") is True)
@postcondition(lambda s, r: r["tenant_id"] == s.get("current_tenant"))
def search_database(query): ...

session = Session(state={"authenticated": True, "current_tenant": "t_9"})
session.register(search_database)
```

## Offline analysis / CI mode

`Session.replay` runs a recorded trace and returns **every** violation without
executing anything — the basis for the coming `acel replay trace.json` CLI and
for testing contracts against known-bad traces.

```python
violations = session.replay([
    {"tool": "delete_record", "args": {"id": "1"}},
])
```

## Live MCP proxy (Phase 2)

ACEL can gate a **real** MCP server's tool calls, live, via the official MCP
Python SDK's `ServerMiddleware` hook. Every `tools/call` request passes
through ACEL's gate *before* the real tool handler runs — a blocked call has
zero side effects.

```bash
pip install "acel-core[mcp]"
```

```python
from mcp.server.mcpserver import MCPServer
from acel import Session, must_precede
from acel.mcp_middleware import ACELMiddleware

session = Session()
session.add_contract(must_precede("validate_record", "delete_record"))

server = MCPServer("my-server", middleware=[ACELMiddleware(session)])

@server.tool()
def delete_record(record_id: str) -> dict: ...
```

See `examples/toy_server.py` for a complete toy server (5 tools, 3 contracts)
and `tests/test_mcp_proxy.py` for an end-to-end demo: a real `ClientSession`
talking to this server, with ACEL catching an ordering violation, a
cardinality violation, and a state-precondition violation — each one halted
before the tool it would have run.

## Correctness

```bash
python benchmarks/correctness.py
```

A labeled dataset of 51 synthetic tool-call traces spanning all 6 temporal
templates (valid sequences, violating sequences, and edge cases like empty
traces and multiple simultaneous contracts) — measured at **100% precision
and 100% recall**. Since the monitor is deterministic automaton checking, not
statistical detection, that's the expected result; the suite exists to prove
it and to catch any future regression (it's also wired into `pytest` as
`tests/test_correctness_suite.py`, so a miss fails CI directly).

## Performance

```bash
python benchmarks/latency.py
```

Measured on the reference dev machine, 20,000 iterations, discarding a 1,000-call
warmup: added p95 latency per tool call is **~0.005ms at 1 active contract** and
**~0.04ms at 50 concurrently active contracts** — well under the <5ms target.
Each temporal contract is a deterministic automaton advanced in O(1) per event,
so overhead scales linearly with the number of *active* contracts, not with
session length.

## Tests

```bash
pip install pytest
pytest                    # core monitor + evidence (no extra deps)
pip install "acel-core[mcp]"
pytest tests/test_mcp_proxy.py tests/test_cli_serve.py   # live MCP proxy + CLI
```

## License

MIT
