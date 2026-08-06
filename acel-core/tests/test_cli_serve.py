"""Tests for `acel serve`'s module-loading and wiring logic.

Deliberately does NOT invoke `server.run_stdio_async()` — that blocks on real
stdio and would hang the test suite. Instead this verifies the CLI can find
and load a server module, extract its `build_server()`, and correctly report
the contracts that would be enforced — everything short of the blocking
event loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from acel.cli import _load_module, build_parser

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def test_load_module_from_file_path():
    module = _load_module(str(EXAMPLES_DIR / "toy_server.py"))
    assert hasattr(module, "build_server")


def test_toy_server_build_server_returns_wired_session():
    module = _load_module(str(EXAMPLES_DIR / "toy_server.py"))
    server, session = module.build_server()
    assert server.name == "acel-toy-server"
    contract_specs = [c.spec for c in session.contracts]
    assert any("must_precede" in s for s in contract_specs)
    assert any("at_most_n_times" in s for s in contract_specs)


def test_serve_missing_build_server_reports_error(tmp_path, capsys):
    bad_module = tmp_path / "no_server.py"
    bad_module.write_text("x = 1\n")

    parser = build_parser()
    args = parser.parse_args(["serve", str(bad_module)])
    exit_code = args.func(args)

    assert exit_code == 2
    assert "does not define build_server" in capsys.readouterr().err


def test_serve_subcommand_is_registered():
    parser = build_parser()
    args = parser.parse_args(["serve", "some_module"])
    assert args.command == "serve"
    assert args.module == "some_module"
