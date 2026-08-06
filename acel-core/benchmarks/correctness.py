"""Run the labeled correctness dataset and report precision / recall / accuracy.

Since ACEL's temporal monitor is a deterministic automaton (not a statistical
classifier), the target is 100% on every metric — any miss here is a real bug,
not measurement noise. Exits non-zero if accuracy is not 100%, so this doubles
as a regression gate.

Run: python benchmarks/correctness.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from correctness_dataset import CASES

from acel import Session
from acel.registry import build_contract


def run_case(case) -> bool:
    """Return True if ACEL found at least one violation in this trace."""
    session = Session(halt_on_violation=False)
    for spec in case.contracts:
        session.add_contract(build_contract(spec))
    violations = session.replay(case.trace)
    return len(violations) > 0


def main() -> None:
    tp = fp = tn = fn = 0
    mismatches = []

    for case in CASES:
        found = run_case(case)
        if case.expect_violation and found:
            tp += 1
        elif case.expect_violation and not found:
            fn += 1
            mismatches.append((case, "MISSED a real violation (false negative)"))
        elif not case.expect_violation and found:
            fp += 1
            mismatches.append((case, "FALSE ALARM on a valid trace (false positive)"))
        else:
            tn += 1

    total = len(CASES)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    accuracy = (tp + tn) / total if total else 1.0

    print(f"ACEL correctness suite — {total} labeled traces\n")
    print(f"  True positives  (caught real violations): {tp}")
    print(f"  True negatives  (correctly passed valid):  {tn}")
    print(f"  False positives (false alarms):            {fp}")
    print(f"  False negatives (missed violations):       {fn}")
    print()
    print(f"  Precision: {precision:.1%}")
    print(f"  Recall:    {recall:.1%}")
    print(f"  Accuracy:  {accuracy:.1%}")

    if mismatches:
        print(f"\n{len(mismatches)} MISMATCH(ES):")
        for case, reason in mismatches:
            print(f"  [{case.id}] {reason}")
        raise SystemExit(1)

    print("\nAll cases correct.")


if __name__ == "__main__":
    main()
