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

**New here? Read Install → Quickstart → The eight temporal templates and
you'll have a working contract in a few minutes.** Everything past that is
reference material for specific needs (MCP, multi-tenant servers, config
files, evidence/signing, metrics) — jump to whichever section matches what
you're trying to do:

- [Install](#install) · [Quickstart](#quickstart) · [The eight temporal
  templates](#the-eight-temporal-templates) · [Pre/postconditions](#prepostconditions-with-decorators)
- [Using ACEL outside MCP](#using-acel-outside-mcp-langchain-openai-function-calling-or-anything-else)
  (LangChain / OpenAI function calling / any Python callable)
- [Live MCP proxy](#live-mcp-proxy-phase-2) · [Multiple simultaneous
  clients](#multiple-simultaneous-clients) (multi-tenant servers)
- [Shadow mode](#shadow-mode) (safe rollout) · [Config-driven
  contracts](#config-driven-contracts-no-code-required) (no-code rules files)
- [Verifying evidence for tampering](#verifying-evidence-for-tampering) ·
  [Security notes](#security-notes) (read this before production use)
- [Metrics](#metrics) · [Concurrency](#concurrency) · [Correctness](#correctness)
  / [Performance](#performance) (the numbers behind the claims)

## Install

```bash
pip install acel-core
```

Requires Python 3.10+. No required dependencies for the core monitor — `mcp`,
`pyyaml` (config files), `cryptography` (Ed25519 signing), and
`langchain-core` are optional, installed as needed (see the sections that use
them below, or grab everything with `pip install "acel-core[mcp,config,sign,langchain]"`).

Working from a clone of this repo instead (e.g. to run the tests or examples)?

```bash
git clone https://github.com/Purav-Kanda/Acel
cd Acel/acel-core
pip install -e ".[dev,mcp,config,sign,langchain]"
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

## The eight temporal templates

| Template | Meaning |
| --- | --- |
| `must_precede(a, b)` | every `b` must be preceded by some `a` |
| `at_most_n_times(a, n)` | `a` occurs at most `n` times per session |
| `at_most_total(a, field, limit)` | the sum of `args[field]` across all calls to `a` must not exceed `limit` |
| `never_after(a, b)` | `a` must never occur after `b` |
| `required_before_session_end(a)` | `a` must occur at least once before the session ends |
| `cannot_follow_without(a, b)` | `a` may not occur unless `b` occurred earlier |
| `mutually_exclusive(a, b)` | `a` and `b` must not both occur in one session |
| `rate_limit(a, n, window_seconds)` | `a` occurs at most `n` times in any rolling `window_seconds`-second window |

Each template is a deterministic automaton advanced in O(1) (amortized, for
`rate_limit`) per tool call, with a three-valued verdict (`SATISFIED` /
`VIOLATED` / `UNKNOWN`) over the finite trace. `at_most_total` and
`rate_limit` are the two templates that read more than just the tool
name: `at_most_total` sums a numeric argument field across calls (e.g. a
payment amount) and fails closed — a call missing the field, or with a
non-numeric value there, is treated as a violation rather than silently let
through, since silently ignoring an unreadable amount would be the actually
dangerous failure mode for a spend cap. `rate_limit` tracks wall-clock
timestamps of recent matching calls instead of a running session total, so
it caps *bursts* (calls per minute) rather than a per-session budget — the
two compose if you want both.

```python
from acel import Session, at_most_total, rate_limit

session = Session()
session.add_contract(at_most_total("send_payment", "amount", limit=500))
session.add_contract(rate_limit("send_payment", n=3, window_seconds=60))

session.call("send_payment", {"amount": 300}, result={"sent": True})
session.call("send_payment", {"amount": 250}, result={"sent": True})  # raises: 550 > 500
```

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

## Using ACEL outside MCP: LangChain, OpenAI function calling, or anything else

`Session.call()` is already framework-agnostic — MCP is just one caller of
it, not a requirement. `acel.adapters` has two small helpers that save you
the boilerplate of matching a specific framework's tool-call shape onto it:

```python
from acel import Session, must_precede
from acel.adapters import guard

session = Session()
session.add_contract(must_precede("validate_record", "delete_record"))

# Wrap a plain function (a LangChain tool's `func=`, a dispatch-table
# callable, anything called as `the_tool(**kwargs)`) so every invocation
# is gated first — same enforcement, same evidence log, no MCP involved.
guarded_delete = guard(session, "delete_record", delete_record)
```

For a hand-rolled agent loop around an OpenAI-compatible chat completions
API, `guard_openai_tool_call` gates a raw `tool_calls[i]` entry directly:

```python
from acel.adapters import guard_openai_tool_call

for tool_call in response.choices[0].message.tool_calls:
    result = guard_openai_tool_call(session, tool_call, TOOLS[tool_call.function.name])
```

Full runnable demos: `examples/langchain_agent_example.py` (requires
`pip install "acel-core[langchain]"`) and
`examples/openai_function_calling_example.py` (no extra dependency, no API
key needed to run it — uses hand-built `tool_calls` dicts shaped like the
real API's response).

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

### Multiple simultaneous clients

`ACELMiddleware(session)` above wires one fixed `Session` shared by every
client that connects — fine for local testing or a server that only ever
has one client at a time, but unsafe once more than one client can connect
at once: every connection would read and write the same state, contracts,
and trace, so one client's calls could trip another client's rules or one
client's authentication could leak into another's session.

For a server meant to serve more than one client at once, pass
`session_factory` instead of `session`: ACEL builds a brand-new, fully
isolated `Session` the first time each connection is seen, and reuses it
for the rest of that connection's requests. Different connections never
share state, contracts, or trace.

```python
from acel.mcp_middleware import ACELMiddleware

def build_session() -> Session:
    session = Session(state={"authenticated": False})
    session.add_contract(must_precede("authenticate", "read_user_data"))
    return session

middleware = ACELMiddleware(session_factory=build_session)
server = MCPServer("my-server", middleware=[middleware])
```

Sessions are tracked internally by the MCP SDK's own per-connection
`Connection` object via a weak-reference map, so a session is released as
soon as its connection closes rather than accumulating forever on a
long-running server. See `examples/multi_tenant_server.py` for a complete
runnable example and `tests/test_multi_tenant.py` for an end-to-end proof
against two real, simultaneous `ClientSession` connections: one client
authenticates, its own follow-up call succeeds, and the *other* client's
identical call is still blocked, proving zero state leakage between them.

## Shadow mode

The recommended way to roll out a new set of contracts: **shadow mode**
detects and records every violation exactly as enforce mode does — same
evidence log, same hash chain — but never blocks a call. Run it against real
traffic first, see what it would have caught, then switch to enforce once
you trust the rules.

```python
session = Session(mode="shadow")  # default is "enforce"
```

```bash
acel serve examples/toy_server.py --shadow
```

`Session.call()`, `.precheck()`/`.postcheck()` (the MCP proxy path), and the
CLI all respect `mode`. `Session.replay()` does not — it's a retrospective
CI-gate tool ("would this recorded trace have been blocked"), not a live
session, so it always reports every violation regardless of mode.

## Config-driven contracts (no code required)

Temporal contracts can be declared in a plain JSON or YAML file instead of
Python — useful for trying ACEL against your own tools without writing any
code, or for keeping the rule set separate from your server implementation:

```bash
acel init-config rules.yaml     # writes a starter file
acel validate rules.yaml        # parses it, prints the contracts it declares
```

```yaml
state:
  authenticated: false

contracts:
  - template: must_precede
    args: [validate_record, delete_record]
  - template: at_most_n_times
    args: [send_payment]
    kwargs: {n: 1}
```

Layer a rules file on top of a live server (`--contracts` adds to whatever
`build_server()` already sets up, and merges the `state` block in):

```bash
acel serve examples/toy_server.py --contracts rules.yaml
```

Or check a recorded trace against a rules file directly (the same format
`acel replay` has always used, now also parseable as YAML):

```bash
acel replay trace.json --rules rules.yaml
```

**Why preconditions/postconditions aren't in the config file:** they
evaluate real logic over state (`lambda s: s.get("authenticated") is True`),
and there's no safe way to deserialize arbitrary logic from a data file
without either an `eval`-style security hole or a bespoke expression
language. Temporal contracts have no such problem — every template is fully
described by tool names and simple parameters, so building one from a config
file is just constructing an object from validated data, no code execution
involved. Pre/postconditions stay in Python, wired directly to your tools —
install YAML support with `pip install "acel-core[config]"`.

### Naming a bundle of contracts as a group

Purely organizational — a group isn't a new kind of contract or a change to
enforcement, it's a name for a bundle of contracts you keep referring to
together. Worth it once a server has enough rules that "these three are the
refund policy" is worth saying out loud:

```python
session = Session()
session.add_contract_group("refund_policy", [
    must_precede("verify_customer", "issue_refund"),
    at_most_total("issue_refund", "amount", limit=500),
])

session.groups                        # {"refund_policy": [...]}
session.contracts_in_group("refund_policy")
```

Or declare it once in a rules file and pull it into `contracts` wherever
it's needed with `{group: name}`:

```yaml
groups:
  refund_policy:
    - template: must_precede
      args: [verify_customer, issue_refund]
    - template: at_most_total
      args: [issue_refund, amount]
      kwargs: {limit: 500}

contracts:
  - template: must_precede
    args: [open_ticket, close_ticket]
  - group: refund_policy
```

`acel validate` shows group membership alongside the flat contract list. A
group declared but never referenced from `contracts` has no effect — it's
inert until something pulls it in.

## Verifying evidence for tampering

Every violation is recorded as a tamper-evident, hash-chained bundle. Save
one to disk and check it later — from a completely fresh process, with no
in-memory state — with `acel verify`:

```bash
acel replay trace.json --rules rules.json --save-evidence evidence.json
acel verify evidence.json
```

```
OK — 3 bundle(s) verified. Hash chain is intact, no tampering detected.
```

That checks hash-chain *consistency* — SHA-256 is unkeyed, so on its own it
can't prove *authenticity* against someone who can edit the file (they can
just recompute the hashes too). If you signed the log (`ed25519_signer`,
see Security notes below), always pass the public key to actually check
the signature:

```bash
acel verify evidence.json --public-key 4f2e...c19a
```

```
OK — 3 bundle(s) verified. Hash chain is intact and every signature checks out, no tampering detected.
```

If any field in any bundle was altered after the fact, `acel verify` fails
and reports the exact bundle index where the chain first breaks — everything
from that point onward is untrustworthy, but pinpointing *where* it broke is
what actually helps you investigate:

```
FAIL — tampering detected. Bundle 1 (of 5) is the first to break the chain...
```

To actually *look at* what's in an evidence log, rather than just check its
integrity, use `acel show` — a human-readable timeline instead of raw JSON:

```bash
acel show evidence.json --trace
```

```
ACEL Evidence Log — evidence.json
1 bundle(s), chain OK

[0] 2026-08-06T18:07:34.466187+00:00  step 1
    kind:     temporal
    contract: must_precede(validate_record, delete_record)
    tool:     delete_record
    args:     {"id": "1"}
    trace (0 call(s) leading up to this):
    hash:     fab359c33c…  (unsigned)
```

`--trace` prints the full call history leading up to each violation, not
just the offending call; drop it for a shorter summary. If the chain is
broken, `acel show` marks the exact bundle where it happened the same way
`acel verify` does.

## Metrics

Opt in to Prometheus-style metrics by passing a `Metrics` instance to `Session`:

```python
from acel import Session, Metrics, must_precede

metrics = Metrics()
session = Session(metrics=metrics)
session.add_contract(must_precede("validate_record", "delete_record"))

# ... handle real traffic ...

print(metrics.render_prometheus())
```

Tracks call volume, gate latency (the same thing `benchmarks/latency.py`
measures offline, but live from your own traffic), and violation counts —
both overall by kind and broken down per contract, so you can see *which*
rule is actually tripping in production, not just that something did:

```
acel_calls_total 142

acel_violations_total{kind="temporal"} 3
acel_violations_total{kind="precondition"} 1
acel_violations_total{kind="postcondition"} 0

acel_contract_violations_total{contract="must_precede(validate_record, delete_record)"} 3

acel_gate_latency_seconds_count 142
acel_gate_latency_seconds_sum 0.000312
```

`render_prometheus()` just returns a string — serve it however fits your
deployment (a `/metrics` route on whatever web framework fronts your
server, a sidecar, a log line). If you don't already have an HTTP server to
hang a route off of, `serve_metrics_http(metrics, port=9090)` starts a
minimal stdlib-only one for you. Entirely opt-in: leave `metrics` unset and
none of this bookkeeping runs.

## Concurrency

A `Session` is safe to call from more than one thread or async task at
once. Every method that mutates shared state (`call`, `precheck`,
`postcheck`, `replay`, `end_session`) is guarded by an internal
`threading.RLock`, so concurrent callers can't corrupt a contract's
internal counters, the step counter, or the trace — verified with real
`ThreadPoolExecutor`-driven tests firing dozens of concurrent calls at a
shared `at_most_n_times`/`at_most_total`/`rate_limit` contract and checking
for lost updates (`tests/test_concurrency.py`). The lock is never held
across an `await`: the MCP middleware's `precheck()` → *(real tool
runs, unguarded)* → `postcheck()` split exists specifically so a slow tool
call doesn't serialize every other in-flight request on the same
connection.

This is about safety within *one* `Session`, not about sharing one
`Session` across multiple clients — for that, see multi-tenant
`session_factory` support above, which gives each connection its own
fully isolated `Session` in the first place.

## Security notes

- **Evidence bundles embed full call arguments, results, and state
  snapshots by default.** That's what makes them useful evidence, but it
  also means anything sensitive passed as a tool argument (a password, a raw
  token, a secret) ends up persisted verbatim if you save an evidence log to
  disk or share it — unless you opt into redaction. Pass `redact_fields=` to
  `Session` (or directly to `EvidenceLog`) with the dict-key names you
  consider sensitive, and any matching value anywhere in a violation's
  args/result/trace/state — nested dicts and per-call trace entries
  included — is replaced with a short, non-reversible hash marker *before*
  the bundle is hashed or signed:

  ```python
  session = Session(redact_fields={"password", "api_key", "ssn"})
  ```

  Two redacted entries with the same original value still produce the same
  marker (so "this session reused the same token twice" stays visible to an
  auditor). The marker is an **HMAC-SHA256 keyed with a random, in-memory-only
  key** that `EvidenceLog` generates once and never writes to the log —
  recovering the original value from the marker requires that key, so an
  attacker who only has the evidence log (the threat this feature defends
  against) can't dictionary-attack it offline, even for a short/guessable
  value like a PIN. (If you call `redact_violation()` directly without going
  through `Session`/`EvidenceLog`, it defaults to an *unkeyed* SHA-256 marker
  instead — safe for correlation and for high-entropy secrets, but
  brute-forceable for low-entropy ones; pass your own `key=` bytes if you
  need the same protection outside `EvidenceLog`.) Fields you don't list are
  left alone, so still prefer keeping secrets out of tool *arguments*
  entirely where you can — pass a reference/ID and resolve the real secret
  inside your own tool implementation instead.
- **`ed25519_signer()` can persist its key, or stay ephemeral — your
  choice.** Called with no arguments, it generates a fresh, unpersisted key
  every time (fine for signing within one process's lifetime, but restart
  and old signatures stop matching the new public key). Called with a path
  (`ed25519_signer("~/.acel/signing_key.bin")`), it generates the key once,
  writes it to that file with owner-only permissions (`0o600`), and reuses
  it on every future call with the same path — so the public key, and every
  signature made against it, stays verifiable across restarts:

  ```python
  sign, public_key_hex = ed25519_signer("~/.acel/signing_key.bin")
  session = Session(signer=sign)
  ```

  Treat that key file exactly like an SSH private key: back it up if you
  need old signatures to keep verifying, and never commit it to a repo or
  evidence log.
- **The hash chain alone proves consistency, not authenticity — always
  verify with the public key if signing is enabled.** `EvidenceLog.verify()`
  and `acel verify`/`acel show` recompute SHA-256 hash links, which is
  enough to catch accidental corruption, but SHA-256 is an unkeyed function:
  anyone who can edit the evidence-log file can also recompute every hash
  from an edited bundle onward, and the chain will still "verify" with no
  key required. If you're signing evidence (see above), always pass the
  public key so the signature is actually checked, not just its presence:

  ```bash
  acel verify evidence.json --public-key <hex from ed25519_signer>
  acel show evidence.json --public-key <hex from ed25519_signer>
  ```

  Without `--public-key`, both commands still run and print a warning —
  useful for a quick corruption check, but it is not tamper-evidence against
  a party who can write to the file.
- **State-based preconditions are not safe against concurrent/pipelined
  calls to the same tool — use temporal contracts for anything that needs
  to hold under concurrency.** A precondition only reads `session.state`;
  the state isn't updated until `postcheck` commits it, after the real tool
  has run. If a client has two calls to the same tool in flight at once
  (which MCP allows, and which `Session` supports via its
  `precheck`/`postcheck` split), both can read the same pre-commit state and
  both pass — so a precondition like `lambda s: s["balance"] >= amount` can
  let two concurrent calls both pass against a balance that should only
  cover one of them. Temporal contracts (`at_most_total`, `rate_limit`,
  `at_most_n_times`) don't have this gap, because their counters are
  mutated synchronously under the lock during `precheck` itself — express
  spend caps, quantity limits, and rate limits as temporal contracts, not as
  a hand-written precondition, if concurrent calls are possible.
- **Config files (`--rules`, `--contracts`) are parsed with `yaml.safe_load`
  and `json.loads` only** — never `yaml.load` or `eval`. There is no code
  execution path from a rules file; that's exactly why pre/postconditions
  can't be declared there (see above) — only tool names, counts, and plain
  values are ever deserialized.

## Correctness

```bash
python benchmarks/correctness.py
```

A labeled dataset of 67 synthetic tool-call traces spanning all 8 temporal
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

## Testing against a real agent, not a script

Everything above proves ACEL works against scripted tool calls. For the
stronger version — a real LLM in Claude Desktop or Claude Code actually
driving the tool calls, and ACEL blocking a mistake the model made itself —
see [`docs/TESTING_WITH_REAL_AGENTS.md`](docs/TESTING_WITH_REAL_AGENTS.md).
It walks through wiring up `examples/support_agent_server.py` (a realistic
customer-support/refund scenario) and gives adversarial prompts designed to
actually trigger each contract.

## License

MIT
