"""Tests for Session.add_contract_group — named bundles of contracts."""

from __future__ import annotations

import pytest

from acel import Session, at_most_total, must_precede


def test_add_contract_group_registers_every_contract():
    session = Session(halt_on_violation=False)
    group = session.add_contract_group(
        "refund_policy",
        [
            must_precede("verify_customer", "issue_refund"),
            at_most_total("issue_refund", "amount", limit=500),
        ],
    )
    assert len(group) == 2
    assert len(session.contracts) == 2
    assert set(session.contracts) == set(group)


def test_group_contracts_enforce_exactly_like_individually_added_ones():
    session = Session(halt_on_violation=False)
    session.add_contract_group(
        "refund_policy",
        [must_precede("verify_customer", "issue_refund")],
    )
    violation = session.call("issue_refund", {"amount": 100})
    assert violation.kind == "temporal"
    assert "verify_customer" in violation.spec


def test_groups_property_returns_named_bundles():
    session = Session()
    g1 = session.add_contract_group("a", [must_precede("x", "y")])
    g2 = session.add_contract_group("b", [must_precede("p", "q"), must_precede("r", "s")])

    groups = session.groups
    assert set(groups) == {"a", "b"}
    assert groups["a"] == g1
    assert groups["b"] == g2


def test_groups_property_excludes_ungrouped_contracts():
    session = Session()
    session.add_contract(must_precede("x", "y"))  # not part of any group
    session.add_contract_group("a", [must_precede("p", "q")])

    assert len(session.contracts) == 2  # both are enforced
    assert list(session.groups) == ["a"]  # but only the named one shows up here


def test_contracts_in_group_returns_the_right_contracts():
    session = Session()
    session.add_contract_group("a", [must_precede("x", "y")])
    contracts = session.contracts_in_group("a")
    assert len(contracts) == 1
    assert contracts[0].spec == "must_precede(x, y)"


def test_contracts_in_group_raises_for_unknown_group():
    session = Session()
    with pytest.raises(KeyError):
        session.contracts_in_group("nope")


def test_duplicate_group_name_raises_value_error():
    session = Session()
    session.add_contract_group("a", [must_precede("x", "y")])
    with pytest.raises(ValueError, match="already"):
        session.add_contract_group("a", [must_precede("p", "q")])


def test_groups_returns_copies_not_live_references():
    session = Session()
    session.add_contract_group("a", [must_precede("x", "y")])
    snapshot = session.groups
    snapshot["a"].clear()  # mutate the returned copy
    assert len(session.groups["a"]) == 1  # session's own bookkeeping is untouched
