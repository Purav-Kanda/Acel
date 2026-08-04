"""The ``acel`` command-line interface.

Ships two commands:

- ``acel replay`` — check a recorded tool-call trace against a set of
  temporal contracts, offline. Intended for CI: exits non-zero if any
  contract is violated, so a bad agent trace fails the build.
- ``acel serve`` — run a live MCP server with ACEL enforcement wired in,
  over stdio. The contracts live in your server module's own
  ``build_server()`` function (see ``examples/toy_server.py``), the same way
  you'd actually deploy ACEL: as code, not a config file. ``replay``'s JSON
  rules format is deliberately a separate, CI-oriented workflow.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from .registry import build_contract
from .session import Session


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def cmd_replay(args: argparse.Namespace) -> int:
    rules = _load_json(args.rules)
    trace = _load_json(args.trace)

    session = Session(state=rules.get("initial_state"), halt_on_violation=False)
    for spec in rules.get("contracts", []):
        session.add_contract(build_contract(spec))

    violations = session.replay(trace)

    if not violations:
        print(f"OK  — {len(trace)} events checked, no violations.")
        return 0

    print(f"FAIL — {len(violations)} violation(s) in {len(trace)} events:")
    for v in violations:
        print(f"  [step {v.step}] {v.kind}: {v.spec}  (tool={v.tool})")
    if args.evidence:
        print("\nEvidence bundle:")
        print(session.evidence.to_json())
    return 1


def _load_module(ref: str) -> ModuleType:
    """Load a server module from either a file path or a dotted module name.

    Raises ``FileNotFoundError`` (for a ``.py`` path that doesn't exist) or
    ``ImportError`` (for a dotted module name that can't be imported) with a
    plain, single-line message — callers turn these into clean CLI errors
    rather than a raw traceback.
    """
    path = Path(ref)
    if ref.endswith(".py"):
        if not path.exists():
            raise FileNotFoundError(f"no such file: {ref}")
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load module from path: {ref}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return importlib.import_module(ref)


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        from mcp.server.mcpserver import MCPServer  # noqa: F401  (availability check)
    except ImportError:
        print(
            "error: `acel serve` requires the optional MCP dependency.\n"
            "        Install it with: pip install \"acel-core[mcp]\"",
            file=sys.stderr,
        )
        return 2

    try:
        module = _load_module(args.module)
    except (FileNotFoundError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not hasattr(module, "build_server"):
        print(
            f"error: {args.module!r} does not define build_server() -> (MCPServer, Session).\n"
            f"        See examples/toy_server.py for the expected shape.",
            file=sys.stderr,
        )
        return 2

    server, session = module.build_server()
    contract_names = ", ".join(c.spec for c in session.contracts) or "(none)"
    print(f"ACEL serving '{server.name}' over stdio.", file=sys.stderr)
    print(f"Active contracts: {contract_names}", file=sys.stderr)
    print("Press Ctrl+C to stop.\n", file=sys.stderr)

    import asyncio

    try:
        asyncio.run(server.run_stdio_async())
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acel",
        description="Agent Contract Enforcement Layer — runtime verification for agent tool calls.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    replay = sub.add_parser("replay", help="Check a recorded trace against contracts.")
    replay.add_argument("trace", help="Path to a JSON trace: a list of {tool, args, result}.")
    replay.add_argument("--rules", required=True, help="Path to a JSON rules file.")
    replay.add_argument(
        "--evidence",
        action="store_true",
        help="Print the hash-chained evidence bundle for any violations.",
    )
    replay.set_defaults(func=cmd_replay)

    serve = sub.add_parser("serve", help="Run a live, ACEL-enforced MCP server over stdio.")
    serve.add_argument(
        "module",
        help="Path to a server module (or dotted module name) defining build_server().",
    )
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    main()
