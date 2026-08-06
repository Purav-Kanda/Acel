"""A single ACEL-enforced MCP server correctly handling multiple simultaneous
clients, each with their own fully isolated Session.

This is the pattern to follow for any real deployment that might have more
than one client connected at once (the other example servers in this repo
are deliberately kept single-session for simplicity, since they're meant for
local testing and demos — see the note in acel/mcp_middleware.py for why
that's unsafe once more than one client can connect).

The tools are the same authenticate/read_user_data pair as toy_server.py, so
the difference to look at is entirely in build_server(): the middleware is
constructed with `session_factory=` instead of a fixed `session=`, so ACEL
builds a brand-new Session — its own state, its own contracts, its own
trace, its own evidence log — the first time each client connects, and
never lets two clients' calls affect each other.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from acel import Session, must_precede
from acel.mcp_middleware import ACELMiddleware


def build_session(mode: str = "enforce") -> Session:
    """Called once per new connection — every client gets a fresh one of these."""
    session = Session(state={"authenticated": False}, halt_on_violation=False, mode=mode)
    session.add_contract(must_precede("authenticate", "read_user_data"))
    session.register_tool(
        "authenticate",
        commit=lambda s, args, result: s.set("authenticated", bool(result.get("ok"))),
    )
    session.register_tool(
        "read_user_data",
        precondition=lambda s: s.get("authenticated") is True,
    )
    return session


def build_server(mode: str = "enforce") -> tuple[MCPServer, ACELMiddleware]:
    """Unlike the other example servers, this returns the middleware, not a
    single Session — there is no one shared Session to hand back. Use
    `middleware.sessions()` to introspect all currently-live per-connection
    sessions (mainly useful for tests; a real deployment doesn't normally
    need this)."""
    middleware = ACELMiddleware(session_factory=lambda: build_session(mode=mode))
    server = MCPServer("acel-multi-tenant-server", middleware=[middleware])

    @server.tool()
    def authenticate(user: str) -> dict:
        """Log a user in. Always succeeds in this toy server."""
        return {"ok": True, "user": user}

    @server.tool()
    def read_user_data(query: str) -> dict:
        """Read data. Gated by ACEL: requires a prior successful authenticate()
        *on this same connection* — another client's authenticate() call has
        zero effect here, unlike the single-session examples."""
        return {"rows": [{"query": query, "value": "secret-row"}]}

    return server, middleware


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    server, _ = build_server()
    asyncio.run(server.run_stdio_async())
