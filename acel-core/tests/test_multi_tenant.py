"""Proves per-connection session isolation against the real MCP SDK: two
separate ClientSession connections to the *same* running server instance
must not see each other's state, contracts violations, or trace at all.

This is the actual credibility test for the session_factory feature — a
unit test against ACELMiddleware's internal dict would prove the bookkeeping
logic works, but this proves the SDK's own per-connection identity (the
thing the whole feature is keyed on) really does what the docstring in
mcp_middleware.py claims it does, end to end.
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

from multi_tenant_server import build_server


async def _connect(server):
    """Open one real client<->server connection over in-memory streams and
    return (client_session, server_task) — caller is responsible for
    cancelling server_task and exiting the client's async context."""
    ctx = create_client_server_memory_streams()
    client_streams, server_streams = await ctx.__aenter__()
    client_read, client_write = client_streams
    server_read, server_write = server_streams

    async def run_server():
        await server._lowlevel_server.run(
            server_read,
            server_write,
            server._lowlevel_server.create_initialization_options(),
        )

    server_task = asyncio.create_task(run_server())
    client = ClientSession(client_read, client_write)
    await client.__aenter__()
    await client.initialize()
    return client, server_task, ctx


async def _disconnect(client, server_task, ctx):
    await client.__aexit__(None, None, None)
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass
    await ctx.__aexit__(None, None, None)


def run(coro):
    return asyncio.run(coro)


def test_two_clients_get_fully_separate_sessions():
    server, middleware = build_server()
    assert middleware.is_per_connection is True
    assert middleware.sessions() == []  # nobody connected yet

    async def scenario():
        client_a, task_a, ctx_a = await _connect(server)
        client_b, task_b, ctx_b = await _connect(server)
        try:
            # Client A authenticates; client B never does.
            result_a1 = await client_a.call_tool("authenticate", {"user": "alice"})
            assert result_a1.is_error is not True

            # A's read succeeds — A really did authenticate on its own connection.
            result_a2 = await client_a.call_tool("read_user_data", {"query": "q"})
            assert result_a2.is_error is not True

            # B's read must still be BLOCKED — A's authenticate() must not
            # leak into B's session just because they share one server.
            result_b1 = await client_b.call_tool("read_user_data", {"query": "q"})
            assert result_b1.is_error is True
            assert "ACEL HALT" in result_b1.content[0].text
        finally:
            # Must close in reverse (LIFO) order relative to how they were
            # opened — both connections' internal cancel scopes live on this
            # same task's stack, and anyio requires exiting them in the exact
            # reverse of entry order, regardless of which "connection" they
            # logically belong to.
            await _disconnect(client_b, task_b, ctx_b)
            await _disconnect(client_a, task_a, ctx_a)

    run(scenario())

    # Two distinct Session objects were created, one per connection.
    assert len(middleware.sessions()) == 2
    states = sorted(s.state.get("authenticated") for s in middleware.sessions())
    assert states == [False, True]  # exactly one session ever got authenticated


def test_sessions_do_not_accumulate_forever():
    """Once a connection's ServerSession is garbage-collected, its ACEL
    Session should be released too (the WeakKeyDictionary doing its job) —
    otherwise a long-running multi-tenant server would leak memory forever."""
    import gc

    server, middleware = build_server()

    async def one_short_lived_connection():
        client, task, ctx = await _connect(server)
        await client.call_tool("authenticate", {"user": "temp"})
        # Check *while still connected*: exactly one live session.
        assert len(middleware.sessions()) == 1
        await _disconnect(client, task, ctx)

    run(one_short_lived_connection())

    gc.collect()
    # After the connection is fully closed and gc'd, the weak-keyed entry
    # should be gone too — this is the actual leak-prevention guarantee.
    assert len(middleware.sessions()) == 0
    assert type(middleware._sessions_by_connection).__name__ == "WeakKeyDictionary"
