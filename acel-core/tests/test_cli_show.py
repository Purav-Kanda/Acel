"""Tests for `acel show`."""

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


def _save_evidence(tmp_path, trace=BAD_TRACE, rules=RULES):
    rules_path = _write(tmp_path / "rules.json", rules)
    trace_path = _write(tmp_path / "trace.json", trace)
    evidence_path = tmp_path / "evidence.json"

    parser = build_parser()
    args = parser.parse_args(
        ["replay", trace_path, "--rules", rules_path, "--save-evidence", str(evidence_path)]
    )
    args.func(args)
    return evidence_path


def test_show_prints_one_entry_per_violation(tmp_path, capsys):
    evidence_path = _save_evidence(tmp_path)

    parser = build_parser()
    args = parser.parse_args(["show", str(evidence_path)])
    exit_code = args.func(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "[0]" in out
    assert "must_precede(validate_record, delete_record)" in out
    assert "delete_record" in out
    assert "chain OK" in out


def test_show_reports_broken_chain(tmp_path, capsys):
    evidence_path = _save_evidence(tmp_path)

    bundles = json.loads(evidence_path.read_text())
    bundles[0]["violation"]["tool"] = "tampered"
    evidence_path.write_text(json.dumps(bundles))

    parser = build_parser()
    args = parser.parse_args(["show", str(evidence_path)])
    exit_code = args.func(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "CHAIN BROKEN" in out


def test_show_trace_flag_prints_call_history(tmp_path, capsys):
    trace = [
        {"tool": "validate_record", "args": {"id": "1"}, "result": {"ok": True}},
        {"tool": "delete_record", "args": {"id": "1"}, "result": None},
        {"tool": "delete_record", "args": {"id": "1"}, "result": None},
    ]
    rules = {
        "contracts": [
            {"template": "at_most_n_times", "args": ["delete_record"], "kwargs": {"n": 1}},
        ]
    }
    evidence_path = _save_evidence(tmp_path, trace=trace, rules=rules)

    parser = build_parser()
    args = parser.parse_args(["show", str(evidence_path), "--trace"])
    exit_code = args.func(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "trace (" in out
    assert "validate_record" in out


def test_show_without_trace_flag_omits_call_history(tmp_path, capsys):
    evidence_path = _save_evidence(tmp_path)

    parser = build_parser()
    args = parser.parse_args(["show", str(evidence_path)])
    args.func(args)
    out = capsys.readouterr().out

    assert "trace (" not in out


def test_show_empty_evidence_log(tmp_path, capsys):
    path = _write(tmp_path / "empty.json", [])

    parser = build_parser()
    args = parser.parse_args(["show", path])
    exit_code = args.func(args)

    assert exit_code == 0
    assert "0 bundles" in capsys.readouterr().out


def test_show_non_list_json_reports_error(tmp_path, capsys):
    path = _write(tmp_path / "bad.json", {"not": "a list"})

    parser = build_parser()
    args = parser.parse_args(["show", path])
    exit_code = args.func(args)

    assert exit_code == 2
    assert "must contain a JSON list" in capsys.readouterr().err


def test_show_missing_file_reports_error(tmp_path, capsys):
    parser = build_parser()
    args = parser.parse_args(["show", str(tmp_path / "nope.json")])
    exit_code = args.func(args)

    assert exit_code == 2
    assert "could not read" in capsys.readouterr().err


def test_show_subcommand_registered():
    parser = build_parser()
    args = parser.parse_args(["show", "some_file.json"])
    assert args.command == "show"
    assert args.evidence_file == "some_file.json"
    assert args.trace is False


def test_show_subcommand_default_public_key_is_none():
    parser = build_parser()
    args = parser.parse_args(["show", "some_file.json"])
    assert args.public_key is None


def test_show_with_public_key_reports_verified_signature(tmp_path, capsys):
    import pytest

    pytest.importorskip("cryptography")
    from acel import Session, must_precede
    from acel.evidence import ed25519_signer

    sign, pub = ed25519_signer()
    session = Session(halt_on_violation=False, signer=sign)
    session.add_contract(must_precede("validate", "delete"))
    session.call("delete", {"id": "1"})
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(session.evidence.to_json())

    parser = build_parser()
    args = parser.parse_args(["show", str(evidence_path), "--public-key", pub])
    exit_code = args.func(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "chain + signatures OK" in out
    assert "signature verified" in out


def test_show_redacted_evidence_still_renders(tmp_path, capsys):
    from acel import Session, must_precede

    session = Session(halt_on_violation=False, redact_fields={"secret"})
    session.add_contract(must_precede("validate", "delete"))
    session.call("delete", {"id": "1", "secret": "shh"})
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(session.evidence.to_json())

    parser = build_parser()
    args = parser.parse_args(["show", str(evidence_path)])
    exit_code = args.func(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "REDACTED" in out
    assert "shh" not in out
