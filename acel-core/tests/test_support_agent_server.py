"""End-to-end integration test for examples/support_agent_server.py — the
realistic demo server meant for wiring into a real MCP client (Claude
Desktop, Claude Code) for testing ACEL against a real agent, not just a
scripted one. Proves the server itself is correctly wired before anyone
points a real LLM at it.
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

from support_agent_server import REFUND_CAP, build_server


async def _run_client(server, handler):
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
    return asyncio.run(coro)


def test_valid_refund_flow_reaches_the_real_tools():
    server, acel_session = build_server()

    async def scenario(client: ClientSession):
        r1 = await client.call_tool("open_ticket", {"customer_id": "c_1"})
        assert r1.is_error is not True
        r2 = await client.call_tool("verify_customer", {"ticket_id": "T-c_1"})
        assert r2.is_error is not True
        r3 = await client.call_tool("issue_refund", {"ticket_id": "T-c_1", "amount": 100})
        assert r3.is_error is not True
        r4 = await client.call_tool("close_ticket", {"ticket_id": "T-c_1"})
        assert r4.is_error is not True

    run(_run_client(server, scenario))
    assert [row["tool"] for row in acel_session.trace] == [
        "open_ticket", "verify_customer", "issue_refund", "close_ticket",
    ]
    assert acel_session.evidence.verify() is True


def test_refund_without_verification_is_blocked():
    server, acel_session = build_server()

    async def scenario(client: ClientSession):
        await client.call_tool("open_ticket", {"customer_id": "c_1"})
        result = await client.call_tool("issue_refund", {"ticket_id": "T-c_1", "amount": 50})
        assert result.is_error is True
        assert "ACEL HALT" in result.content[0].text

    run(_run_client(server, scenario))
    assert [row["tool"] for row in acel_session.trace] == ["open_ticket"]
    assert len(acel_session.evidence) == 1


def test_refund_over_the_cumulative_cap_is_blocked():
    """The credibility test for at_most_total specifically: two refunds that
    are each individually fine, but whose sum exceeds REFUND_CAP."""
    server, acel_session = build_server()

    async def scenario(client: ClientSession):
        await client.call_tool("open_ticket", {"customer_id": "c_1"})
        await client.call_tool("verify_customer", {"ticket_id": "T-c_1"})
        first = await client.call_tool(
            "issue_refund", {"ticket_id": "T-c_1", "amount": REFUND_CAP - 100}
        )
        assert first.is_error is not True
        second = await client.call_tool(
            "issue_refund", {"ticket_id": "T-c_1", "amount": 150}
        )  # pushes cumulative total over REFUND_CAP
        assert second.is_error is True
        assert second.structured_content["violation"]["kind"] == "temporal"

    run(_run_client(server, scenario))
    assert [row["tool"] for row in acel_session.trace] == [
        "open_ticket", "verify_customer", "issue_refund",
    ]


def test_close_ticket_without_opening_is_blocked():
    server, acel_session = build_server()

    async def scenario(client: ClientSession):
        result = await client.call_tool("close_ticket", {"ticket_id": "T-ghost"})
        assert result.is_error is True

    run(_run_client(server, scenario))
    assert acel_session.trace == []


def test_refund_after_ticket_closed_is_blocked_by_state_precondition():
    """Second layer of defense: even with verification done, a closed ticket
    should not accept a refund — this is the state-precondition check, not
    the temporal one, since ordering-wise verify_customer did happen first."""
    server, acel_session = build_server()

    async def scenario(client: ClientSession):
        await client.call_tool("open_ticket", {"customer_id": "c_1"})
        await client.call_tool("verify_customer", {"ticket_id": "T-c_1"})
        await client.call_tool("close_ticket", {"ticket_id": "T-c_1"})
        result = await client.call_tool("issue_refund", {"ticket_id": "T-c_1", "amount": 20})
        assert result.is_error is True
        assert result.structured_content["violation"]["kind"] == "precondition"

    run(_run_client(server, scenario))


def test_escalate_is_never_gated():
    server, acel_session = build_server()

    async def scenario(client: ClientSession):
        result = await client.call_tool(
            "escalate_to_human", {"ticket_id": "T-anything", "reason": "angry customer"}
        )
        assert result.is_error is not True

    run(_run_client(server, scenario))
    assert [row["tool"] for row in acel_session.trace] == ["escalate_to_human"]
