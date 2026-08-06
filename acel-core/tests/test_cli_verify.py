"""Tests for `acel verify` and `acel replay --save-evidence`."""

from __future__ import annotations

import json

from acel.cli import build_parser

RULES = {
    "contracts": [
        {"template": "must_precede", "args": ["validate_record", "delete_record"]},
    ]
}

BAD_TRACE = [{"tool": "delete_record", "args": {"id": "1"}}]


def _write(path, obj):
    path.write_text(json.dumps(obj))
    return str(path)


def test_replay_save_evidence_then_verify_roundtrip(tmp_path, capsys):
    rules_path = _write(tmp_path / "rules.json", RULES)
    trace_path = _write(tmp_path / "trace.json", BAD_TRACE)
    evidence_path = tmp_path / "evidence.json"

    parser = build_parser()
    args = parser.parse_args(
        ["replay", trace_path, "--rules", rules_path, "--save-evidence", str(evidence_path)]
    )
    args.func(args)  # exit code 1 expected (a violation), that's fine here
    capsys.readouterr()

    assert evidence_path.exists()
    bundles = json.loads(evidence_path.read_text())
    assert len(bundles) == 1

    verify_args = parser.parse_args(["verify", str(evidence_path)])
    exit_code = verify_args.func(verify_args)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OK" in out
    assert "no tampering detected" in out


def test_verify_detects_tampering(tmp_path, capsys):
    rules_path = _write(tmp_path / "rules.json", RULES)
    trace_path = _write(tmp_path / "trace.json", BAD_TRACE)
    evidence_path = tmp_path / "evidence.json"

    parser = build_parser()
    args = parser.parse_args(
        ["replay", trace_path, "--rules", rules_path, "--save-evidence", str(evidence_path)]
    )
    args.func(args)
    capsys.readouterr()

    # Tamper with the recorded evidence: change the violation's tool name.
    bundles = json.loads(evidence_path.read_text())
    bundles[0]["violation"]["tool"] = "something_else"
    evidence_path.write_text(json.dumps(bundles))

    verify_args = parser.parse_args(["verify", str(evidence_path)])
    exit_code = verify_args.func(verify_args)

    err = capsys.readouterr().err
    assert exit_code == 1
    assert "FAIL" in err
    assert "Bundle 0" in err


def test_verify_empty_evidence_log_is_ok(tmp_path, capsys):
    path = _write(tmp_path / "empty.json", [])

    parser = build_parser()
    args = parser.parse_args(["verify", path])
    exit_code = args.func(args)

    assert exit_code == 0
    assert "0 bundles" in capsys.readouterr().out


def test_verify_non_list_json_reports_error(tmp_path, capsys):
    path = _write(tmp_path / "bad.json", {"not": "a list"})

    parser = build_parser()
    args = parser.parse_args(["verify", path])
    exit_code = args.func(args)

    assert exit_code == 2
    assert "must contain a JSON list" in capsys.readouterr().err


def test_verify_malformed_bundle_reports_error(tmp_path, capsys):
    path = _write(tmp_path / "bad.json", [{"not": "a real bundle"}])

    parser = build_parser()
    args = parser.parse_args(["verify", path])
    exit_code = args.func(args)

    assert exit_code == 2
    assert "malformed field" in capsys.readouterr().err


def test_verify_missing_file_reports_error(tmp_path, capsys):
    parser = build_parser()
    args = parser.parse_args(["verify", str(tmp_path / "nope.json")])
    exit_code = args.func(args)

    assert exit_code == 2
    assert "could not read" in capsys.readouterr().err


def test_verify_subcommand_registered():
    parser = build_parser()
    args = parser.parse_args(["verify", "some_file.json"])
    assert args.command == "verify"
    assert args.evidence_file == "some_file.json"
