"""Tests for the `acel validate` and `acel init-config` subcommands, and for
`acel serve --contracts` layering config-declared contracts onto a server
module's own build_server()."""

from __future__ import annotations

from pathlib import Path

import pytest

from acel.cli import build_parser

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"

VALID_RULES = """\
{
  "state": {"authenticated": false},
  "contracts": [
    {"template": "must_precede", "args": ["validate_record", "delete_record"]}
  ]
}
"""


def test_validate_valid_rules_file(tmp_path, capsys):
    path = tmp_path / "rules.json"
    path.write_text(VALID_RULES)

    parser = build_parser()
    args = parser.parse_args(["validate", str(path)])
    exit_code = args.func(args)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OK" in out
    assert "must_precede" in out


def test_validate_bad_rules_file_reports_error(tmp_path, capsys):
    path = tmp_path / "rules.json"
    path.write_text('{"contracts": [{"template": "nonsense"}]}')

    parser = build_parser()
    args = parser.parse_args(["validate", str(path)])
    exit_code = args.func(args)

    assert exit_code == 2
    assert "error" in capsys.readouterr().err


def test_init_config_writes_starter_file(tmp_path):
    path = tmp_path / "rules.yaml"

    parser = build_parser()
    args = parser.parse_args(["init-config", str(path)])
    exit_code = args.func(args)

    assert exit_code == 0
    assert path.exists()
    assert "contracts:" in path.read_text()


def test_init_config_refuses_to_overwrite_without_force(tmp_path, capsys):
    path = tmp_path / "rules.yaml"
    path.write_text("existing content")

    parser = build_parser()
    args = parser.parse_args(["init-config", str(path)])
    exit_code = args.func(args)

    assert exit_code == 2
    assert "already exists" in capsys.readouterr().err
    assert path.read_text() == "existing content"


def test_init_config_force_overwrites(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("existing content")

    parser = build_parser()
    args = parser.parse_args(["init-config", str(path), "--force"])
    exit_code = args.func(args)

    assert exit_code == 0
    assert "contracts:" in path.read_text()


def test_init_config_default_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = build_parser()
    args = parser.parse_args(["init-config"])
    exit_code = args.func(args)

    assert exit_code == 0
    assert (tmp_path / "rules.yaml").exists()


def test_serve_contracts_flag_layers_extra_contracts(tmp_path):
    pytest.importorskip("mcp")
    rules_path = tmp_path / "extra.json"
    rules_path.write_text(
        '{"contracts": [{"template": "mutually_exclusive", '
        '"args": ["send_payment", "delete_record"]}]}'
    )

    parser = build_parser()
    args = parser.parse_args(
        ["serve", str(EXAMPLES_DIR / "toy_server.py"), "--contracts", str(rules_path)]
    )
    # Reproduce the wiring cmd_serve does, short of actually running the server.
    import inspect

    from acel.cli import _load_module, config_mod

    module = _load_module(args.module)
    server, session = module.build_server()
    original_count = len(session.contracts)

    extra = config_mod.contracts_from_rules(config_mod.load_rules(args.contracts))
    session.add_contracts(extra)

    assert len(session.contracts) == original_count + 1
    assert any("mutually_exclusive" in c.spec for c in session.contracts)
    del inspect  # unused, kept for parity with cmd_serve's own imports
