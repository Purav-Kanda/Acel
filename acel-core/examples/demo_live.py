"""Interactive, watch-it-happen demo: a real MCP client talking to a real
ACEL-enforced MCP server, over a real subprocess — not the in-memory shortcut
the test suite uses.

Run it directly:

    python examples/demo_live.py

It spawns `toy_server.py` as a separate process (genuine stdio IPC, the same
transport Claude Desktop/Claude Code use to talk to a real MCP server), then
fires a scripted sequence of tool calls at it — some valid, some deliberately
rule-breaking — printing each result as it happens so you can watch ACEL
block a call live.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_SCRIPT = str(Path(__file__).parent / "toy_server.py")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


async def call(client: ClientSession, tool: str, args: dict) -> None:
    print(f"{BOLD}>>> {tool}({args}){RESET}")
    result = await client.call_tool(tool, args)
    if result.is_error:
        text = result.content[0].text if result.content else "(no detail)"
        print(f"{RED}    BLOCKED — {text}{RESET}")
    else:
        payload = result.structured_content
        if payload is None and result.content:
            payload = result.content[0].text
        print(f"{GREEN}    OK — {payload}{RESET}")
    print()
    await asyncio.sleep(0.4)  # pace the output so it's easy to follow


async def fresh_session():
    """Spawn a brand-new server subprocess and connect to it.

    Each scenario gets its own process (and therefore its own ACEL Session)
    so violations from one scenario don't carry over into the next. Note:
    this is a demo-script convenience, not an ACEL limitation — a single
    temporal contract is deliberately *sticky* once violated (see
    TemporalContract.on_event): a real deployment should treat "this
    session had a contract violation" as a signal to stop trusting the rest
    of that session too, not silently recover.
    """
    params = StdioServerParameters(command=sys.executable, args=[SERVER_SCRIPT])
    cm = stdio_client(params)
    read, write = await cm.__aenter__()
    client = ClientSession(read, write)
    await client.__aenter__()
    await client.initialize()
    return client, cm


async def run_scenario(title: str, steps: list[tuple[str, dict]], note: str = "") -> None:
    print(f"{BOLD}--- {title} ---{RESET}")
    if note:
        print(f"{YELLOW}{note}{RESET}")
    print()
    client, cm = await fresh_session()
    try:
        for tool, args in steps:
            await call(client, tool, args)
    finally:
        await client.__aexit__(None, None, None)
        await cm.__aexit__(None, None, None)


async def main() -> None:
    print(f"{YELLOW}Each scenario below spawns a fresh copy of the toy MCP server{RESET}")
    print(f"{YELLOW}as a real subprocess, so every scenario starts from a clean session.{RESET}\n")

    await run_scenario(
        "Scenario 1: valid sequence",
        [
            ("authenticate", {"user": "purav"}),
            ("read_user_data", {"query": "SELECT * FROM orders"}),
        ],
    )

    await run_scenario(
        "Scenario 2: read before authenticate (state precondition)",
        [("read_user_data", {"query": "SELECT * FROM orders"})],
        note="No authenticate() first -> ACEL must block this before the real tool runs.",
    )

    await run_scenario(
        "Scenario 3: delete before validate (temporal ordering)",
        [("delete_record", {"record_id": "r_42"})],
        note="No validate_record() first -> ACEL must block this.",
    )

    await run_scenario(
        "Scenario 4: validate, then delete (correct order -> allowed)",
        [
            ("validate_record", {"record_id": "r_42"}),
            ("delete_record", {"record_id": "r_42"}),
        ],
    )

    await run_scenario(
        "Scenario 5: send_payment once, then again (cardinality cap)",
        [
            ("send_payment", {"amount": 100}),
            ("send_payment", {"amount": 100}),
        ],
        note="at_most_n_times(send_payment, n=1) -> the second call must be blocked.",
    )

    print(f"{YELLOW}Demo complete.{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
