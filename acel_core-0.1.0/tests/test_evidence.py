"""Tests for the hash-chained evidence log."""

from __future__ import annotations

import copy

from acel import Session, at_most_n_times, must_precede
from acel.evidence import EvidenceLog, GENESIS_HASH
from acel.violations import Violation


def _violation(step: int, spec: str = "must_precede(a, b)") -> Violation:
    return Violation(kind="temporal", spec=spec, tool="b", step=step)


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
