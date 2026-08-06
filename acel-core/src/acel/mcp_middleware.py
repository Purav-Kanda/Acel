"""ACEL as real MCP server middleware — the live enforcement point.

This wires one or more :class:`~acel.session.Session` instances into the
official MCP Python SDK's request pipeline via ``ServerMiddleware``. Every
``tools/call`` request the server receives passes through
:meth:`ACELMiddleware.__call__` *before* the tool's own handler runs: ACEL
gates the call there, and only calls ``call_next`` (which dispatches to the
real tool) if the gate passes. On a violation, the real handler is never
invoked — the halt happens before any effect the tool would have — and a
structured error result is returned to the client instead.

This module is optional and requires the ``mcp`` package
(``pip install acel-core[mcp]``); the base ``acel`` package has no dependency
on it.

**Single-session vs. per-connection.** ``ACELMiddleware(session)`` wires one
fixed ``Session`` shared by every client that connects to the server — fine
for local testing, a demo, or any server that only ever has one client at a
time (which covers most of the examples in this repo). It is *not* safe for
a server that serves more than one client at once: every connection would
read and write the same state store, the same temporal contracts, and the
same trace, so one client's calls could trip another client's rules, or
one client's authentication could "leak" into another's session. For a
server meant to handle multiple simultaneous clients,
``ACELMiddleware(session_factory=...)`` builds one fresh, fully isolated
``Session`` per connection instead. It's keyed by the SDK's underlying
``Connection`` object rather than the ``ServerSession`` handed to
middleware — ``ServerSession`` is rebuilt fresh on every single request
(even within the same connection), so it can't identify a connection; the
``Connection`` it wraps is the thing that's actually stable for as long as
the client stays connected. Held via a weak-reference map, so a session is
released once its connection closes rather than accumulating forever.
"""

from __future__ import annotations

import json
import weakref
from typing import Any, Callable

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from mcp_types import CallToolResult, TextContent

from .session import Session
from .violations import Violation

SessionFactory = Callable[[], Session]


def _first_text_block(blocks: list[Any]) -> str | None:
    """Find the first text content block, whether it's a model or a raw dict.

    Depending on where in the request pipeline a middleware runs, the SDK may
    hand back either validated ``TextContent`` models or their pre-validation
    wire-format dicts (``{"type": "text", "text": ...}``). Handle both.
    """
    for block in blocks:
        if isinstance(block, TextContent):
            return block.text
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text")
    return None


def _extract_result(handler_result: HandlerResult) -> Any:
    """Pull a plain value out of a tool-call result for postcondition checks.

    Prefers structured content (what a tool declaring ``structured_output``
    returns). Most tools don't opt into that, so the SDK instead serializes
    their return value as a JSON text block — this tries to parse that as
    JSON, falling back to the raw text only if it isn't. Handles both the
    validated ``CallToolResult`` model and its raw pre-serialization dict
    form (``{"content": [...], "isError": ...}``), since a context-tier
    middleware may see either depending on the transport.
    """
    if isinstance(handler_result, CallToolResult):
        structured = handler_result.structured_content
        blocks: list[Any] = handler_result.content
    elif isinstance(handler_result, dict):
        structured = handler_result.get("structuredContent") or handler_result.get("structured_content")
        blocks = handler_result.get("content", [])
    else:
        return handler_result

    if structured is not None:
        return structured

    text = _first_text_block(blocks)
    if text is None:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"text": text}


def _halt_result(violation: Violation) -> CallToolResult:
    """Build the structured error the client sees when ACEL blocks a call."""
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=f"ACEL HALT: {violation.message}",
            )
        ],
        structured_content={
            "acel_halted": True,
            "violation": violation.to_dict(),
        },
        is_error=True,
    )


class ACELMiddleware(ServerMiddleware[Any]):
    """Runtime-verification middleware: gate every ``tools/call`` through a Session.

    Non-tool requests (``tools/list``, ``initialize``, etc.) pass through
    untouched. For ``tools/call``, this is the exact enforcement point: the
    real tool handler is skipped entirely on a violation, so a blocked call
    has zero side effects.

    Provide exactly one of:

    - ``session`` — a single ``Session`` shared by every connection. Simple,
      and correct as long as the server only ever has one client at a time.
    - ``session_factory`` — a zero-argument callable that builds a fresh
      ``Session`` (e.g. ``lambda: build_session(mode="enforce")``). A new
      one is created the first time a given connection is seen, and reused
      for the rest of that connection's requests; different connections
      never share state, contracts, or trace.
    """

    def __init__(
        self,
        session: Session | None = None,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        if (session is None) == (session_factory is None):
            raise ValueError(
                "ACELMiddleware requires exactly one of `session` or `session_factory`, "
                f"got session={session!r}, session_factory={session_factory!r}"
            )
        self._shared_session = session
        self._session_factory = session_factory
        # Keyed by the SDK's own per-connection Connection object (identity,
        # not id()/int reuse) so a finished connection's Session is released
        # once nothing else references its Connection — no manual cleanup,
        # no unbounded growth over a long-running server's lifetime.
        self._sessions_by_connection: "weakref.WeakKeyDictionary[Any, Session]" = (
            weakref.WeakKeyDictionary()
        )

    @property
    def is_per_connection(self) -> bool:
        return self._session_factory is not None

    def sessions(self) -> list[Session]:
        """All Sessions currently tracked (one shared, or one per live connection).

        Mainly for tests/introspection — a real deployment doesn't normally
        need to enumerate these.
        """
        if self._shared_session is not None:
            return [self._shared_session]
        return list(self._sessions_by_connection.values())

    def _session_for(self, ctx: ServerRequestContext[Any, Any]) -> Session:
        if self._shared_session is not None:
            return self._shared_session
        assert self._session_factory is not None  # guaranteed by __init__'s check
        # ctx.session (a ServerSession) is a *new object on every single
        # request*, even within the same client connection - confirmed by a
        # diagnostic that printed id(ctx.session) for several requests inside
        # one connection and got a different id every time. The thing that's
        # actually stable for the lifetime of one connection is the private
        # `_connection` (a `Connection`) that every one of those ServerSession
        # wrappers is built around, so that's what we key on instead.
        connection = ctx.session._connection
        found = self._sessions_by_connection.get(connection)
        if found is None:
            found = self._session_factory()
            self._sessions_by_connection[connection] = found
        return found

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        if ctx.method != "tools/call" or ctx.params is None:
            return await call_next(ctx)

        tool = ctx.params.get("name")
        arguments = ctx.params.get("arguments") or {}
        if not isinstance(tool, str):
            return await call_next(ctx)

        session = self._session_for(ctx)

        gate = session.precheck(tool, arguments)
        if not gate.allowed:
            return _halt_result(gate.violation)

        result = await call_next(ctx)

        violation = session.postcheck(tool, gate, _extract_result(result))
        if violation is not None:
            return _halt_result(violation)

        return result
