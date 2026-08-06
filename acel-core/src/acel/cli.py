"""The ``acel`` command-line interface.

Ships five commands:

- ``acel replay`` — check a recorded tool-call trace against a set of
  temporal contracts, offline. Intended for CI: exits non-zero if any
  contract is violated, so a bad agent trace fails the build.
- ``acel validate`` — parse a rules file (JSON or YAML) and print the
  contracts it declares, without running anything. A quick sanity check
  before wiring a rules file into ``replay`` or ``serve``.
- ``acel verify`` — check a saved evidence-log JSON file (e.g. from
  ``replay --evidence --save-evidence``) for tampering, by recomputing its
  hash chain from scratch. Reports exactly which bundle broke the chain, if
  any did.
- ``acel show`` — pretty-print a saved evidence-log JSON file as a
  human-readable timeline: one entry per violation, in order, with the
  contract that broke, the call that broke it, and the chain-verification
  status — for actually looking at what ACEL caught, instead of reading raw
  JSON.
- ``acel serve`` — run a live MCP server with ACEL enforcement wired in,
  over stdio. Contracts can come from your server module's own
  ``build_server()`` function (see ``examples/toy_server.py``) and/or from a
  ``--contracts`` rules file layered on top — the file only ever declares
  temporal ordering rules and initial state (plain data), never code;
  pre/postconditions still require Python since they evaluate logic over
  state, not just names and counts (see ``acel/config.py`` for why).
- ``acel init-config`` — write a starter rules file to edit.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from . import config as config_mod
from .evidence import EvidenceLog
from .session import Session


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def cmd_replay(args: argparse.Namespace) -> int:
    try:
        rules = config_mod.load_rules(args.rules)
    except config_mod.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    trace = _load_json(args.trace)

    session = Session(state=config_mod.state_from_rules(rules), halt_on_violation=False)
    try:
        session.add_contracts(config_mod.contracts_from_rules(rules))
    except config_mod.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    violations = session.replay(trace)

    if args.save_evidence:
        Path(args.save_evidence).write_text(session.evidence.to_json(), encoding="utf-8")
        print(f"Evidence log written to {args.save_evidence!r}.", file=sys.stderr)

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


def cmd_verify(args: argparse.Namespace) -> int:
    bundles, code = _load_evidence_bundles(args.evidence_file)
    if bundles is None:
        return code

    if not bundles:
        print(f"OK — {args.evidence_file} contains 0 bundles (nothing to verify).")
        return 0

    try:
        ok, bad_index = EvidenceLog.verify_bundles_detailed(bundles)
    except (KeyError, TypeError) as exc:
        print(
            f"error: {args.evidence_file!r} doesn't look like a valid evidence log "
            f"(missing or malformed field: {exc})",
            file=sys.stderr,
        )
        return 2

    if ok:
        print(f"OK — {len(bundles)} bundle(s) verified. Hash chain is intact, no tampering detected.")
        return 0

    print(
        f"FAIL — tampering detected. Bundle {bad_index} (of {len(bundles)}) is the first to "
        f"break the chain — its hash doesn't match its own recomputed content, or it doesn't "
        f"link to the previous bundle correctly. Everything from bundle {bad_index} onward "
        f"cannot be trusted.",
        file=sys.stderr,
    )
    return 1


def _short_hash(h: str, length: int = 10) -> str:
    return h[:length] + "…" if len(h) > length else h


def _compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(", ", ": "), default=str)


def _load_evidence_bundles(path: str) -> tuple[list[dict[str, Any]] | None, int]:
    """Shared loader for ``verify``/``show``: read+validate an evidence-log
    JSON file, returning ``(bundles, exit_code)``. ``bundles`` is ``None``
    if loading failed and the caller should just return ``exit_code``."""
    try:
        bundles = _load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not read {path!r}: {exc}", file=sys.stderr)
        return None, 2
    if not isinstance(bundles, list):
        print(f"error: {path!r} must contain a JSON list of evidence bundles", file=sys.stderr)
        return None, 2
    return bundles, 0


def cmd_show(args: argparse.Namespace) -> int:
    bundles, code = _load_evidence_bundles(args.evidence_file)
    if bundles is None:
        return code

    if not bundles:
        print(f"{args.evidence_file}: 0 bundles (no violations recorded).")
        return 0

    try:
        ok, bad_index = EvidenceLog.verify_bundles_detailed(bundles)
    except (KeyError, TypeError) as exc:
        print(
            f"error: {args.evidence_file!r} doesn't look like a valid evidence log "
            f"(missing or malformed field: {exc})",
            file=sys.stderr,
        )
        return 2

    chain_status = "chain OK" if ok else f"CHAIN BROKEN at bundle {bad_index}"
    print(f"ACEL Evidence Log — {args.evidence_file}")
    print(f"{len(bundles)} bundle(s), {chain_status}\n")

    for i, bundle in enumerate(bundles):
        violation = bundle.get("violation", {})
        marker = ""
        if not ok:
            if i == bad_index:
                marker = "  ⚠ CHAIN BROKEN HERE"
            elif i > bad_index:
                marker = "  ⚠ untrustworthy (chain broke earlier)"

        print(f"[{i}] {bundle.get('timestamp', '?')}  step {violation.get('step', '?')}{marker}")
        print(f"    kind:     {violation.get('kind', '?')}")
        print(f"    contract: {violation.get('spec', '?')}")
        print(f"    tool:     {violation.get('tool', '?')}")
        print(f"    args:     {_compact_json(violation.get('args', {}))}")
        if violation.get("result") is not None:
            print(f"    result:   {_compact_json(violation.get('result'))}")
        state = violation.get("state_snapshot") or {}
        if state:
            print(f"    state:    {_compact_json(state)}")
        if args.trace:
            trace = violation.get("trace") or []
            print(f"    trace ({len(trace)} call(s) leading up to this):")
            for entry in trace:
                print(
                    f"      step {entry.get('step', '?')}: {entry.get('tool', '?')}"
                    f"({_compact_json(entry.get('args', {}))}) -> "
                    f"{_compact_json(entry.get('result'))}"
                )
        signed = "signed" if bundle.get("signature") else "unsigned"
        print(f"    hash:     {_short_hash(bundle.get('bundle_hash', '?'))}  ({signed})")
        print()

    return 0 if ok else 1


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

    mode = "shadow" if args.shadow else "enforce"
    build_server_params = inspect.signature(module.build_server).parameters
    if "mode" in build_server_params:
        server, session = module.build_server(mode=mode)
    else:
        if args.shadow:
            print(
                f"warning: {args.module!r}'s build_server() doesn't accept a `mode` "
                f"argument — ignoring --shadow, running in enforce mode.",
                file=sys.stderr,
            )
        server, session = module.build_server()

    if args.contracts:
        try:
            rules = config_mod.load_rules(args.contracts)
            extra_contracts = config_mod.contracts_from_rules(rules)
        except config_mod.ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        session.add_contracts(extra_contracts)
        session.state.update(config_mod.state_from_rules(rules))
        print(
            f"Loaded {len(extra_contracts)} contract(s) from {args.contracts!r}.",
            file=sys.stderr,
        )

    contract_names = ", ".join(c.spec for c in session.contracts) or "(none)"
    print(f"ACEL serving '{server.name}' over stdio, mode={session.mode}.", file=sys.stderr)
    print(f"Active contracts: {contract_names}", file=sys.stderr)
    if session.mode == "shadow":
        print("Shadow mode: violations are logged, nothing is blocked.", file=sys.stderr)
    print("Press Ctrl+C to stop.\n", file=sys.stderr)

    import asyncio

    try:
        asyncio.run(server.run_stdio_async())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        rules = config_mod.load_rules(args.rules)
        contracts = config_mod.contracts_from_rules(rules)
        state = config_mod.state_from_rules(rules)
    except config_mod.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"OK — {args.rules} parses cleanly.")
    print(f"Initial state: {state or '(none)'}")
    if not contracts:
        print("Contracts: (none declared)")
    else:
        print(f"Contracts ({len(contracts)}):")
        for c in contracts:
            print(f"  - {c.spec}")
    return 0


_STARTER_CONFIG = """\
# ACEL rules file — declarative temporal contracts + initial state.
# Load it with `acel validate rules.yaml`, `acel replay trace.json --rules rules.yaml`,
# or `acel serve your_server.py --contracts rules.yaml`.
#
# Templates: must_precede, at_most_n_times, never_after,
# required_before_session_end, cannot_follow_without, mutually_exclusive.
# Pre/postconditions aren't expressible here on purpose — they need real
# logic over state, so they stay in your Python tool registration.

state:
  authenticated: false

contracts:
  - template: must_precede
    args: [validate_record, delete_record]
  - template: at_most_n_times
    args: [send_payment]
    kwargs: {n: 1}
"""


def cmd_init_config(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.exists() and not args.force:
        print(f"error: {path} already exists (use --force to overwrite)", file=sys.stderr)
        return 2
    path.write_text(_STARTER_CONFIG, encoding="utf-8")
    print(f"Wrote a starter rules file to {path}. Edit it, then try: acel validate {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acel",
        description="Agent Contract Enforcement Layer — runtime verification for agent tool calls.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    replay = sub.add_parser("replay", help="Check a recorded trace against contracts.")
    replay.add_argument("trace", help="Path to a JSON trace: a list of {tool, args, result}.")
    replay.add_argument("--rules", required=True, help="Path to a JSON or YAML rules file.")
    replay.add_argument(
        "--evidence",
        action="store_true",
        help="Print the hash-chained evidence bundle for any violations.",
    )
    replay.add_argument(
        "--save-evidence",
        metavar="PATH",
        help="Write the hash-chained evidence log to PATH as JSON, regardless of outcome "
        "(pass this file to `acel verify` later to check it hasn't been tampered with).",
    )
    replay.set_defaults(func=cmd_replay)

    verify = sub.add_parser(
        "verify", help="Check a saved evidence-log JSON file for tampering."
    )
    verify.add_argument(
        "evidence_file", help="Path to a JSON file containing a list of evidence bundles."
    )
    verify.set_defaults(func=cmd_verify)

    show = sub.add_parser(
        "show", help="Pretty-print a saved evidence-log JSON file as a timeline."
    )
    show.add_argument(
        "evidence_file", help="Path to a JSON file containing a list of evidence bundles."
    )
    show.add_argument(
        "--trace",
        action="store_true",
        help="Also print the full call trace leading up to each violation.",
    )
    show.set_defaults(func=cmd_show)

    validate = sub.add_parser(
        "validate", help="Parse a rules file and print the contracts it declares."
    )
    validate.add_argument("rules", help="Path to a JSON or YAML rules file.")
    validate.set_defaults(func=cmd_validate)

    init_config = sub.add_parser(
        "init-config", help="Write a starter rules file to edit."
    )
    init_config.add_argument(
        "path", nargs="?", default="rules.yaml", help="Where to write it (default: rules.yaml)."
    )
    init_config.add_argument(
        "--force", action="store_true", help="Overwrite the file if it already exists."
    )
    init_config.set_defaults(func=cmd_init_config)

    serve = sub.add_parser("serve", help="Run a live, ACEL-enforced MCP server over stdio.")
    serve.add_argument(
        "module",
        help="Path to a server module (or dotted module name) defining build_server().",
    )
    serve.add_argument(
        "--shadow",
        action="store_true",
        help="Run in shadow mode: log violations without blocking any call. "
        "Requires the module's build_server() to accept a `mode` argument.",
    )
    serve.add_argument(
        "--contracts",
        help="Path to a JSON/YAML rules file: extra temporal contracts (and initial "
        "state) layered on top of whatever the module's build_server() already sets up.",
    )
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    main()
