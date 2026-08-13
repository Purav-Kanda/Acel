"""Claude Code hooks integration.

Wires ACEL into Claude Code's ``PreToolUse``/``PostToolUse`` hook events so
it can block a real coding-agent tool call (``Bash``, ``Edit``, ``Write``,
...) before it runs, enforced by the exact same temporal contracts used
everywhere else in this library. See the README section "Guarding Claude
Code itself" for a full setup walkthrough.

**Why this module exists, architecturally:** every other ACEL integration
(the MCP proxy, the LangChain/OpenAI adapters) holds one live ``Session`` in
memory for the lifetime of a connection or process, so contract state
(counters, ordering, etc.) just accumulates naturally as calls come in.
Claude Code hooks don't offer that — each hook fires as a **fresh
subprocess** per tool call, so there is no in-memory state to carry over
between one ``PreToolUse`` invocation and the next.

Instead, this module reconstructs state on every invocation:

1. Load the trace of every tool call already recorded for this Claude Code
   session — a small JSON file this module maintains itself, one per
   ``session_id`` (see :func:`_trace_path`). Nothing here reads or depends
   on Claude Code's own transcript format; it's a private, ACEL-owned log.
2. Rebuild a fresh ``Session`` from your rules file and replay that trace
   through it with :meth:`~acel.session.Session.replay_prefix` — this
   reconstructs exactly the state a long-lived session would be in, without
   falsely triggering end-of-session checks (this is a mid-session replay,
   not a complete one).
3. For ``PreToolUse``: run :meth:`~acel.session.Session.precheck` against
   the new call and print a ``permissionDecision: "deny"`` JSON to stdout
   if it would violate a contract — Claude Code reads this from the hook's
   stdout and blocks the tool call before it ever runs.
4. For ``PostToolUse``: append the now-completed call to the trace file, so
   the *next* ``PreToolUse`` invocation sees it when it replays.

**Scope note:** ACEL's temporal contracts key off tool *name* (and, for
``at_most_total``, one numeric argument field) — they don't inspect
arbitrary argument content. That means this integration is a good fit for
things like "at most N ``Bash`` calls per minute" (catching a runaway retry
loop) or "at most N calls to a custom deploy tool per session," but it
can't (yet) express content-based rules like "block any ``Bash`` command
containing ``rm -rf``" — that would need argument-aware preconditions,
which aren't part of the current precondition API (preconditions only see
session state, not the current call's args).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import config as config_mod
from .session import Session

DEFAULT_STATE_DIR = ".claude/acel_state"


def _safe_session_id(session_id: str) -> str:
    """Sanitize a session_id for safe use as a filename — defends against a
    session_id containing path separators or other filesystem-unsafe
    characters, whatever its actual source."""
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return safe or "unknown"


def _trace_path(state_dir: Path, session_id: str) -> Path:
    return state_dir / f"{_safe_session_id(session_id)}.json"


def _load_trace(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_trace(path: Path, trace: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trace), encoding="utf-8")


def _build_session(rules_path: str) -> Session:
    rules = config_mod.load_rules(rules_path)
    session = Session(state=config_mod.state_from_rules(rules), halt_on_violation=False)
    session.add_contracts(config_mod.contracts_from_rules(rules))
    return session


def pretooluse(
    hook_input: dict[str, Any],
    *,
    rules_path: str,
    state_dir: str | Path = DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Core ``PreToolUse`` logic. Returns the JSON dict to print to stdout —
    an empty dict means "allow, no decision to report" (Claude Code's normal
    permission flow then applies); a populated dict carries a
    ``permissionDecision: "deny"`` that blocks the tool call.

    Separated from stdin/stdout handling (see :func:`main_pretooluse`) so it
    can be unit tested directly with a plain dict, no subprocess involved.
    """
    session_id = hook_input.get("session_id", "unknown")
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input") or {}

    trace_file = _trace_path(Path(state_dir), session_id)
    prior_trace = _load_trace(trace_file)

    session = _build_session(rules_path)
    session.replay_prefix(prior_trace)

    gate = session.precheck(tool_name, tool_input)
    if gate.violation is not None and gate.blocking:
        v = gate.violation
        reason = f"ACEL blocked this call — contract {v.spec!r} ({v.kind}) would be violated."
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    return {}


def posttooluse(
    hook_input: dict[str, Any],
    *,
    state_dir: str | Path = DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Core ``PostToolUse`` logic: record the now-completed call into this
    session's trace file so the next :func:`pretooluse` invocation sees it
    when it replays. Never blocks — the tool has already run by the time
    ``PostToolUse`` fires, so there's nothing left to prevent; this is
    purely bookkeeping. Always returns ``{}`` (no decision to report).
    """
    session_id = hook_input.get("session_id", "unknown")
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input") or {}
    tool_response = hook_input.get("tool_response")

    trace_file = _trace_path(Path(state_dir), session_id)
    trace = _load_trace(trace_file)
    trace.append({"tool": tool_name, "args": tool_input, "result": tool_response})
    _save_trace(trace_file, trace)
    return {}


def main_pretooluse(rules_path: str, state_dir: str = DEFAULT_STATE_DIR) -> int:
    """CLI entry point for ``acel hook-pretooluse``: read the hook's JSON
    input from stdin, run :func:`pretooluse`, print any resulting JSON
    decision to stdout. Always exits 0 — a deny is communicated through the
    printed JSON, per Claude Code's documented PreToolUse pattern, not
    through a non-zero exit code.
    """
    hook_input = _read_stdin_json()
    output = pretooluse(hook_input, rules_path=rules_path, state_dir=state_dir)
    if output:
        print(json.dumps(output))
    return 0


def main_posttooluse(state_dir: str = DEFAULT_STATE_DIR) -> int:
    """CLI entry point for ``acel hook-posttooluse``: read stdin, record the
    completed call. Always exits 0.
    """
    hook_input = _read_stdin_json()
    posttooluse(hook_input, state_dir=state_dir)
    return 0


def _read_stdin_json() -> dict[str, Any]:
    """Read+parse JSON from stdin, tolerating a leading UTF-8 BOM.

    Piping a string into a subprocess's stdin from PowerShell prepends a
    UTF-8 BOM (U+FEFF) that plain ``json.loads`` chokes on — Git Bash and
    real Claude Code hook invocations don't add one, but a PowerShell-based
    hook `command` in ``settings.json`` legitimately could, so this strips
    it defensively rather than assuming a particular shell.
    """
    raw = sys.stdin.read().lstrip(chr(0xFEFF))
    return json.loads(raw) if raw.strip() else {}
