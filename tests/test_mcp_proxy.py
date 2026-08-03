"""End-to-end integration test: a real MCP client talks to a real MCP server,
through ACEL's middleware, over an in-process transport.

This is the Phase 2 proof: ACEL intercepts the actual JSON-RPC ``tools/call``
message before the toy server's handler runs, and a violating call never
reaches the tool at all.

Requires the optional ``mcp`` dependency: ``pip install acel-core[mcp]``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

sys.path.insert(0, str(Path(__file__).parent.parent / "examples"))

from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from toy_server import build_server


async def _run_client(server, handler):
    """Boot the toy server and a ClientSession over in-memory streams, then
    hand control to ``handler(session)`` once both sides are initialized."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async def run_server():
            await server._lowlevel_server.run(
                server_read,
                server_write,
                server._lowlevel_server.create_initialization_options(),
            )

        server_task = asyncio.create_task(run_server())
        try:
            async with ClientSession(client_read, client_write) as session:
                await session.initialize()
                await handler(session)
        finally:
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass


def run(coro):
    """Run an async test body to completion. Avoids needing a pytest-asyncio plugin."""
    return asyncio.run(coro)


def test_valid_sequence_reaches_the_real_tool():
    server, acel_session = build_server()

    async def scenario(client: ClientSession):
        result = await client.call_tool("authenticate", {"user": "purav"})
        assert result.is_error is not True
        result = await client.call_tool("read_user_data", {"query": "select *"})
        assert result.is_error is not True

    run(_run_client(server, scenario))
    assert acel_session.state.get("authenticated") is True
    assert [row["tool"] for row in acel_session.trace] == ["authenticate", "read_user_data"]


def test_precondition_violation_halts_before_reaching_the_tool():
    server, acel_session = build_server()

    async def scenario(client: ClientSession):
        # No authenticate() first -> read_user_data must be halted by ACEL.
        result = await client.call_tool("read_user_data", {"query": "select *"})
        assert result.is_error is True
        assert "ACEL HALT" in result.content[0].text

    run(_run_client(server, scenario))
    # The real tool never ran: nothing was ever recorded in the trace.
    assert acel_session.trace == []
    assert len(acel_session.evidence) == 1
    assert acel_session.evidence.verify() is True


def test_temporal_violation_halts_delete_before_validate():
    server, acel_session = build_server()

    async def scenario(client: ClientSession):
        result = await client.call_tool("delete_record", {"record_id": "r_42"})
        assert result.is_error is True
        assert result.structured_content["violation"]["kind"] == "temporal"

    run(_run_client(server, scenario))
    assert acel_session.trace == []  # delete_record's handler never executed


def test_cardinality_violation_halts_second_payment():
    server, acel_session = build_server()

    async def scenario(client: ClientSession):
        first = await client.call_tool("send_payment", {"amount": 10})
        assert first.is_error is not True
        second = await client.call_tool("send_payment", {"amount": 20})
        assert second.is_error is True

    run(_run_client(server, scenario))
    # Only the first payment's execution was ever recorded.
    assert [row["tool"] for row in acel_session.trace] == ["send_payment"]
    assert acel_session.evidence.verify() is True
