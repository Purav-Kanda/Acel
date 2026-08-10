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
    assert args.public_key is None


# ---------------------------------------------------------------------------
# --public-key (security fix: signature verification, not just hash chain)
# ---------------------------------------------------------------------------


def _save_signed_evidence(tmp_path):
    """Build a signed evidence log via the Python API (there's no CLI flag
    to sign during `acel replay`) and write it where the CLI can read it."""
    import pytest

    pytest.importorskip("cryptography")
    from acel import Session, must_precede
    from acel.evidence import ed25519_signer

    sign, pub = ed25519_signer()
    session = Session(halt_on_violation=False, signer=sign)
    session.add_contract(must_precede("validate", "delete"))
    session.call("delete", {"id": "1"})  # violation, gets recorded+signed

    evidence_path = tmp_path / "signed_evidence.json"
    evidence_path.write_text(session.evidence.to_json())
    return evidence_path, pub


def test_verify_without_public_key_warns_but_still_passes(tmp_path, capsys):
    evidence_path, _pub = _save_signed_evidence(tmp_path)

    parser = build_parser()
    args = parser.parse_args(["verify", str(evidence_path)])
    exit_code = args.func(args)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "OK" in captured.out
    assert "no --public-key given" in captured.err or "warning" in captured.err.lower()


def test_verify_with_correct_public_key_passes_and_says_signatures_checked(tmp_path, capsys):
    evidence_path, pub = _save_signed_evidence(tmp_path)

    parser = build_parser()
    args = parser.parse_args(["verify", str(evidence_path), "--public-key", pub])
    exit_code = args.func(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "every signature checks out" in out


def test_verify_with_wrong_public_key_fails(tmp_path, capsys):
    import pytest

    pytest.importorskip("cryptography")
    from acel.evidence import ed25519_signer

    evidence_path, _pub = _save_signed_evidence(tmp_path)
    _sign, unrelated_pub = ed25519_signer()

    parser = build_parser()
    args = parser.parse_args(["verify", str(evidence_path), "--public-key", unrelated_pub])
    exit_code = args.func(args)
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "FAIL" in err


def test_verify_public_key_flag_catches_forged_bundle_that_hash_only_check_misses(tmp_path, capsys):
    """The core regression test for the vulnerability: a forged bundle with
    hashes recomputed to match passes hash-only verify, but fails once a
    public key is supplied."""
    evidence_path, pub = _save_signed_evidence(tmp_path)

    from acel.evidence import _canonical, _payload_of, _sha256_hex

    bundles = json.loads(evidence_path.read_text())
    bundles[0]["violation"]["tool"] = "something_else"
    payload = _payload_of(bundles[0]["index"], bundles[0]["timestamp"], bundles[0]["violation"])
    bundles[0]["trace_hash"] = _sha256_hex(_canonical(payload))
    bundles[0]["bundle_hash"] = _sha256_hex((bundles[0]["prev_hash"] + bundles[0]["trace_hash"]).encode())
    evidence_path.write_text(json.dumps(bundles))

    parser = build_parser()

    hash_only_args = parser.parse_args(["verify", str(evidence_path)])
    assert hash_only_args.func(hash_only_args) == 0  # fooled, as documented

    capsys.readouterr()

    signed_args = parser.parse_args(["verify", str(evidence_path), "--public-key", pub])
    exit_code = signed_args.func(signed_args)
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "FAIL" in err
