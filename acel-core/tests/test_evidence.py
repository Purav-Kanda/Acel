"""Tests for the hash-chained evidence log."""

from __future__ import annotations

import copy

import pytest

from acel import Session, at_most_n_times, must_precede
from acel.evidence import EvidenceLog, GENESIS_HASH, redact_violation
from acel.violations import Violation


def _violation(step: int, spec: str = "must_precede(a, b)") -> Violation:
    return Violation(kind="temporal", spec=spec, tool="b", step=step)


def _violation_with_args(step: int, args: dict, result=None, trace=None) -> Violation:
    return Violation(
        kind="precondition",
        spec="requires(authenticated)",
        tool="read_user_data",
        step=step,
        args=args,
        result=result,
        trace=trace or [],
        state_snapshot={"password": "hunter2", "authenticated": False},
    )


def test_first_bundle_chains_to_genesis():
    log = EvidenceLog()
    bundle = log.record(_violation(1))
    assert bundle.prev_hash == GENESIS_HASH
    assert bundle.index == 0


def test_chain_links_successive_bundles():
    log = EvidenceLog()
    b1 = log.record(_violation(1))
    b2 = log.record(_violation(2))
    assert b2.prev_hash == b1.bundle_hash
    assert b2.index == 1


def test_verify_true_on_untouched_chain():
    log = EvidenceLog()
    log.record(_violation(1))
    log.record(_violation(2))
    assert log.verify() is True


def test_verify_false_when_a_field_is_tampered():
    log = EvidenceLog()
    log.record(_violation(1))
    log.record(_violation(2))
    bundles = [b.to_dict() for b in log.bundles]
    tampered = copy.deepcopy(bundles)
    tampered[0]["violation"]["spec"] = "must_precede(x, y)"  # edit history
    assert EvidenceLog.verify_bundles(tampered) is False
    assert EvidenceLog.verify_bundles(bundles) is True  # original untouched


def test_verify_false_when_a_bundle_is_reordered():
    log = EvidenceLog()
    log.record(_violation(1))
    log.record(_violation(2))
    bundles = [b.to_dict() for b in log.bundles]
    swapped = [bundles[1], bundles[0]]
    assert EvidenceLog.verify_bundles(swapped) is False


def test_session_auto_records_evidence_on_violation():
    session = Session(halt_on_violation=False)
    session.add_contract(must_precede("validate", "delete"))
    session.add_contract(at_most_n_times("delete", 1))
    session.call("delete", {"id": "1"})
    assert len(session.evidence) == 1
    assert session.evidence.verify() is True


def test_optional_signer_is_attached_when_provided():
    calls = []

    def fake_signer(data: bytes) -> str:
        calls.append(data)
        return "deadbeef"

    log = EvidenceLog(signer=fake_signer)
    bundle = log.record(_violation(1))
    assert bundle.signature == "deadbeef"
    assert len(calls) == 1


def test_to_json_round_trips_through_verify():
    import json

    log = EvidenceLog()
    log.record(_violation(1))
    log.record(_violation(2))
    loaded = json.loads(log.to_json())
    assert EvidenceLog.verify_bundles(loaded) is True


def test_verify_bundles_detailed_reports_no_break_on_clean_chain():
    log = EvidenceLog()
    log.record(_violation(1))
    log.record(_violation(2))
    bundles = [b.to_dict() for b in log.bundles]
    ok, bad_index = EvidenceLog.verify_bundles_detailed(bundles)
    assert ok is True
    assert bad_index is None


def test_verify_bundles_detailed_pinpoints_first_tampered_bundle():
    log = EvidenceLog()
    log.record(_violation(1))
    log.record(_violation(2))
    log.record(_violation(3))
    bundles = [b.to_dict() for b in log.bundles]
    tampered = copy.deepcopy(bundles)
    tampered[1]["violation"]["spec"] = "tampered"
    ok, bad_index = EvidenceLog.verify_bundles_detailed(tampered)
    assert ok is False
    assert bad_index == 1  # the middle bundle, not the last one it corrupts too


def test_verify_bundles_detailed_reports_first_break_not_last():
    """Tampering with an early bundle breaks every bundle after it too — make
    sure the reported index is the *first* break, not the last."""
    log = EvidenceLog()
    for i in range(5):
        log.record(_violation(i))
    bundles = [b.to_dict() for b in log.bundles]
    tampered = copy.deepcopy(bundles)
    tampered[1]["violation"]["spec"] = "tampered"
    ok, bad_index = EvidenceLog.verify_bundles_detailed(tampered)
    assert ok is False
    assert bad_index == 1


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redact_violation_masks_matching_keys_in_args_and_result():
    v = _violation_with_args(
        1,
        args={"password": "hunter2", "username": "purav"},
        result={"token": "abc123", "ok": True},
    ).to_dict()
    redacted = redact_violation(v, {"password", "token"})
    assert redacted["args"]["password"] != "hunter2"
    assert redacted["args"]["password"].startswith("***REDACTED(")
    assert redacted["args"]["username"] == "purav"  # untouched
    assert redacted["result"]["token"].startswith("***REDACTED(")
    assert redacted["result"]["ok"] is True  # untouched


def test_redact_violation_masks_nested_and_trace_entries():
    v = _violation_with_args(
        1,
        args={"payload": {"api_key": "sekrit", "note": "hi"}},
        trace=[
            {"step": 1, "tool": "a", "args": {"api_key": "sekrit"}, "result": None},
        ],
    ).to_dict()
    redacted = redact_violation(v, {"api_key"})
    assert redacted["args"]["payload"]["api_key"].startswith("***REDACTED(")
    assert redacted["args"]["payload"]["note"] == "hi"
    assert redacted["trace"][0]["args"]["api_key"].startswith("***REDACTED(")


def test_redact_violation_is_case_insensitive_and_matches_state_snapshot():
    v = _violation_with_args(1, args={}).to_dict()
    redacted = redact_violation(v, {"PASSWORD"})
    assert redacted["state_snapshot"]["password"].startswith("***REDACTED(")
    assert redacted["state_snapshot"]["authenticated"] is False  # untouched


def test_redact_violation_same_value_produces_same_marker():
    v1 = _violation_with_args(1, args={"token": "same-secret"}).to_dict()
    v2 = _violation_with_args(2, args={"token": "same-secret"}).to_dict()
    r1 = redact_violation(v1, {"token"})
    r2 = redact_violation(v2, {"token"})
    assert r1["args"]["token"] == r2["args"]["token"]  # same input -> same marker
    assert r1["args"]["token"] != "same-secret"  # but not the plaintext


def test_redact_violation_with_no_fields_is_a_no_op_copy():
    v = _violation_with_args(1, args={"password": "hunter2"}).to_dict()
    redacted = redact_violation(v, set())
    assert redacted == v
    assert redacted is not v


def test_evidence_log_redacts_before_hashing():
    plain_log = EvidenceLog()
    redacted_log = EvidenceLog(redact_fields={"password"})

    plain_bundle = plain_log.record(_violation_with_args(1, args={"password": "hunter2"}))
    redacted_bundle = redacted_log.record(_violation_with_args(1, args={"password": "hunter2"}))

    assert redacted_bundle.violation["args"]["password"].startswith("***REDACTED(")
    # Redaction changes the payload, so the hash differs from the unredacted chain.
    assert redacted_bundle.trace_hash != plain_bundle.trace_hash
    # The redacted chain is still internally consistent and verifiable.
    assert redacted_log.verify() is True


def test_session_redact_fields_reaches_evidence_log():
    session = Session(halt_on_violation=False, redact_fields={"secret"})
    session.add_contract(must_precede("validate", "delete"))
    session.call("delete", {"id": "1", "secret": "shh"})
    assert len(session.evidence) == 1
    bundle = session.evidence.bundles[0]
    assert bundle.violation["args"]["secret"].startswith("***REDACTED(")


# ---------------------------------------------------------------------------
# Ed25519 key persistence
# ---------------------------------------------------------------------------


def test_ed25519_signer_without_path_generates_new_key_each_call():
    cryptography = pytest.importorskip("cryptography")
    del cryptography
    from acel.evidence import ed25519_signer

    _, pub1 = ed25519_signer()
    _, pub2 = ed25519_signer()
    assert pub1 != pub2  # ephemeral: a fresh keypair every call


def test_ed25519_signer_with_path_persists_and_reuses_key(tmp_path):
    pytest.importorskip("cryptography")
    from acel.evidence import ed25519_signer

    key_file = tmp_path / "acel_signing_key.bin"
    assert not key_file.exists()

    sign1, pub1 = ed25519_signer(key_file)
    assert key_file.exists()

    sign2, pub2 = ed25519_signer(key_file)  # second call reuses the saved key
    assert pub1 == pub2

    # A signature made under the first call still verifies against the
    # second call's signer's key, because it's really the same key.
    sig1 = sign1(b"evidence-bytes")
    sig2 = sign2(b"evidence-bytes")
    assert sig1 == sig2  # same key, same message -> Ed25519 is deterministic


def test_ed25519_signer_key_file_is_owner_only(tmp_path):
    pytest.importorskip("cryptography")
    import stat

    from acel.evidence import ed25519_signer

    key_file = tmp_path / "key.bin"
    ed25519_signer(key_file)
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode == 0o600


# ---------------------------------------------------------------------------
# Signature verification (security fix: hash chain alone is not authenticity)
# ---------------------------------------------------------------------------


def test_verify_without_public_key_still_passes_a_forged_but_recomputed_chain():
    """Documents the known limitation: hash-only verification can't catch a
    forgery where the attacker also recomputes every downstream hash — this
    is exactly why --public-key / public_key_hex exists."""
    pytest.importorskip("cryptography")
    from acel.evidence import ed25519_signer

    sign, _pub = ed25519_signer()
    log = EvidenceLog(signer=sign)
    log.record(_violation(1))
    bundles = [b.to_dict() for b in log.bundles]

    # Forge bundle 0's violation, then recompute its hashes by hand (what an
    # attacker with file write access can trivially do, with no key needed).
    forged = copy.deepcopy(bundles)
    forged[0]["violation"]["spec"] = "must_precede(a, c)"
    from acel.evidence import _canonical, _payload_of, _sha256_hex

    payload = _payload_of(forged[0]["index"], forged[0]["timestamp"], forged[0]["violation"])
    forged[0]["trace_hash"] = _sha256_hex(_canonical(payload))
    forged[0]["bundle_hash"] = _sha256_hex((forged[0]["prev_hash"] + forged[0]["trace_hash"]).encode())
    forged[0]["signature"] = "not-a-real-signature"

    # Hash-chain-only verification is fooled...
    assert EvidenceLog.verify_bundles(forged) is True
    # ...but signature verification catches it.
    assert EvidenceLog.verify_bundles(forged, public_key_hex=_pub) is False


def test_verify_with_public_key_passes_a_genuinely_untampered_signed_log():
    pytest.importorskip("cryptography")
    from acel.evidence import ed25519_signer

    sign, pub = ed25519_signer()
    log = EvidenceLog(signer=sign)
    log.record(_violation(1))
    log.record(_violation(2))
    bundles = [b.to_dict() for b in log.bundles]

    assert EvidenceLog.verify_bundles(bundles, public_key_hex=pub) is True
    assert log.verify(public_key_hex=pub) is True


def test_verify_with_public_key_fails_on_missing_signature():
    pytest.importorskip("cryptography")
    from acel.evidence import ed25519_signer

    sign, pub = ed25519_signer()
    log = EvidenceLog(signer=sign)
    log.record(_violation(1))
    bundles = [b.to_dict() for b in log.bundles]
    bundles[0]["signature"] = None

    ok, bad_index = EvidenceLog.verify_bundles_detailed(bundles, public_key_hex=pub)
    assert ok is False
    assert bad_index == 0


def test_verify_with_public_key_fails_when_unsigned_bundles_are_checked_against_a_key():
    log = EvidenceLog()  # no signer at all
    log.record(_violation(1))
    bundles = [b.to_dict() for b in log.bundles]

    pytest.importorskip("cryptography")
    from acel.evidence import ed25519_signer

    _sign, unrelated_pub = ed25519_signer()
    ok, bad_index = EvidenceLog.verify_bundles_detailed(bundles, public_key_hex=unrelated_pub)
    assert ok is False
    assert bad_index == 0


def test_verify_with_wrong_public_key_fails():
    pytest.importorskip("cryptography")
    from acel.evidence import ed25519_signer

    sign, _pub = ed25519_signer()
    _other_sign, other_pub = ed25519_signer()  # unrelated keypair

    log = EvidenceLog(signer=sign)
    log.record(_violation(1))
    bundles = [b.to_dict() for b in log.bundles]

    ok, bad_index = EvidenceLog.verify_bundles_detailed(bundles, public_key_hex=other_pub)
    assert ok is False
    assert bad_index == 0


# ---------------------------------------------------------------------------
# Keyed (HMAC) redaction markers (security fix: unsalted hash was
# dictionary-attackable for low-entropy values)
# ---------------------------------------------------------------------------


def test_evidence_log_generates_a_random_redact_key_when_redact_fields_set():
    log = EvidenceLog(redact_fields={"password"})
    assert log._redact_key is not None
    assert len(log._redact_key) == 32


def test_evidence_log_uses_hmac_marker_not_plain_sha256():
    from acel.evidence import _redact_marker

    log = EvidenceLog(redact_fields={"password"})
    bundle = log.record(_violation_with_args(1, args={"password": "1234"}))
    marker = bundle.violation["args"]["password"]
    assert marker.startswith("***REDACTED(hmac-sha256:")

    # An offline attacker who only has the evidence log (no key) cannot
    # reproduce the marker just by guessing the plaintext, unlike the old
    # unsalted-sha256 scheme.
    guessed_unkeyed = _redact_marker("1234")
    assert guessed_unkeyed != marker


def test_two_evidence_logs_produce_different_markers_for_the_same_low_entropy_secret():
    """Different random keys per EvidenceLog instance -> markers for the
    exact same guessable value differ across logs, closing off a
    precomputed-dictionary attack that reuses one rainbow table across logs."""
    log_a = EvidenceLog(redact_fields={"pin"})
    log_b = EvidenceLog(redact_fields={"pin"})
    bundle_a = log_a.record(_violation_with_args(1, args={"pin": "0000"}))
    bundle_b = log_b.record(_violation_with_args(1, args={"pin": "0000"}))
    assert bundle_a.violation["args"]["pin"] != bundle_b.violation["args"]["pin"]


def test_redact_violation_direct_call_without_key_still_works_unkeyed():
    """Backward compatible: calling redact_violation() directly (not through
    EvidenceLog) without a key still produces the old-style plain marker."""
    v = _violation_with_args(1, args={"token": "same-secret"}).to_dict()
    redacted = redact_violation(v, {"token"})
    assert redacted["args"]["token"].startswith("***REDACTED(sha256:")


def test_redact_violation_with_explicit_key_produces_hmac_marker():
    v = _violation_with_args(1, args={"token": "x"}).to_dict()
    redacted = redact_violation(v, {"token"}, key=b"\x01" * 32)
    assert redacted["args"]["token"].startswith("***REDACTED(hmac-sha256:")
