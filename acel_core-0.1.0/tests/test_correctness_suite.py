"""Wires the labeled correctness dataset (benchmarks/correctness_dataset.py)
into pytest, so a future regression on any of the 51 cases fails CI directly
— not just the standalone benchmarks/correctness.py report.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "benchmarks"))

from correctness_dataset import CASES

from acel import Session
from acel.registry import build_contract


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_labeled_trace_matches_expected_verdict(case):
    session = Session(halt_on_violation=False)
    for spec in case.contracts:
        session.add_contract(build_contract(spec))
    violations = session.replay(case.trace)
    found = len(violations) > 0
    assert found == case.expect_violation, (
        f"{case.id}: expected violation={case.expect_violation}, got {found}"
    )
