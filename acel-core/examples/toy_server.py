"""A toy MCP tool server used to demonstrate ACEL enforcing contracts live.

Five tools model the classic failure modes from the ACEL build brief:
authenticate -> read_user_data (state-gated read), and
validate_record -> delete_record with a payment cap (temporal ordering +
cardinality). ``build_server`` wires the real official MCP SDK's
``MCPServer`` together with :class:`~acel.mcp_middleware.ACELMiddleware`, so
every one of these tool calls is gated by ACEL before it ever runs.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from acel import Session, at_most_n_times, must_precede
from acel.mcp_middleware import ACELMiddleware


def build_session(mode: str = "enforce") -> Session:
    """The contracts a real deployment would declare for this toy tool server.

    ``mode="shadow"`` detects and logs every violation without ever blocking
    a call — the recommended way to try a new set of contracts against real
    traffic before switching to ``"enforce"``.
    """
    session = Session(state={"authenticated": False}, halt_on_violation=False, mode=mode)
    session.add_contract(must_precede("validate_record", "delete_record"))
    session.add_contract(at_most_n_times("send_payment", n=1))
    session.register_tool(
        "authenticate",
        commit=lambda s, args, result: s.set("authenticated", bool(result.get("ok"))),
    )
    session.register_tool(
        "read_user_data",
        precondition=lambda s: s.get("authenticated") is True,
    )
    return session


def build_server(session: Session | None = None, mode: str = "enforce") -> tuple[MCPServer, Session]:
    session = session or build_session(mode=mode)
    server = MCPServer("acel-toy-server", middleware=[ACELMiddleware(session)])

    @server.tool()
    def authenticate(user: str) -> dict:
        """Log a user in. Always succeeds in this toy server."""
        return {"ok": True, "user": user}

    @server.tool()
    def read_user_data(query: str) -> dict:
        """Read data. Gated by ACEL: requires a prior successful authenticate()."""
        return {"rows": [{"query": query, "value": "secret-row"}]}

    @server.tool()
    def validate_record(record_id: str) -> dict:
        """Mark a record as validated."""
        return {"validated": True, "record_id": record_id}

    @server.tool()
    def delete_record(record_id: str) -> dict:
        """Irreversibly delete a record. Gated by ACEL: must be validated first."""
        return {"deleted": True, "record_id": record_id}

    @server.tool()
    def send_payment(amount: float) -> dict:
        """Send a payment. Gated by ACEL: at most once per session."""
        return {"sent": True, "amount": amount}

    return server, session


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    server, _ = build_server()
    asyncio.run(server.run_stdio_async())
