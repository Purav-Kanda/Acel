"""ACEL as real MCP server middleware — the live enforcement point.

This wires an :class:`~acel.session.Session` directly into the official MCP
Python SDK's request pipeline via ``ServerMiddleware``. Every ``tools/call``
request the server receives passes through :meth:`ACELMiddleware.__call__`
*before* the tool's own handler runs: ACEL gates the call there, and only
calls ``call_next`` (which dispatches to the real tool) if the gate passes.
On a violation, the real handler is never invoked — the halt happens before
any effect the tool would have — and a structured error result is returned
to the client instead.

This module is optional and requires the ``mcp`` package
(``pip install acel-core[mcp]``); the base ``acel`` package has no dependency
on it.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from mcp_types import CallToolResult, TextContent

from .session import Session
from .violations import Violation


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
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        if ctx.method != "tools/call" or ctx.params is None:
            return await call_next(ctx)

        tool = ctx.params.get("name")
        arguments = ctx.params.get("arguments") or {}
        if not isinstance(tool, str):
            return await call_next(ctx)

        gate = self.session.precheck(tool, arguments)
        if not gate.allowed:
            return _halt_result(gate.violation)

        result = await call_next(ctx)

        violation = self.session.postcheck(tool, gate, _extract_result(result))
        if violation is not None:
            return _halt_result(violation)

        return result
