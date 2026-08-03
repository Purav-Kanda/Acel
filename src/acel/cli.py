"""The ``acel`` command-line interface.

Currently ships ``acel replay`` — check a recorded tool-call trace against a set
of temporal contracts, offline. Intended for CI: exits non-zero if any contract
is violated, so a bad agent trace fails the build.
"""

from __future__ import annotations

import argparse
import json
import sys
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
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    main()
