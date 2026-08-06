"""Three-valued verdict for runtime monitoring over finite traces.

Follows the LTL3 semantics of Bauer, Leucker & Schallhart: a monitor observing
a finite prefix of an (unbounded) execution can conclude a property is
permanently satisfied, permanently violated, or still undetermined.
"""

from __future__ import annotations

from enum import Enum


class Verdict(str, Enum):
    """The status of a contract after observing the trace so far.

    - ``SATISFIED``  (LTL3 ``⊤``): the contract holds and can no longer be
      violated by any continuation of the trace.
    - ``VIOLATED``   (LTL3 ``⊥``): the contract has been broken; this verdict
      is permanent (sticky).
    - ``UNKNOWN``    (LTL3 ``?``): the contract is acceptable so far but its
      final status depends on what happens next (or on session end).
    """

    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"

    @property
    def is_violation(self) -> bool:
        return self is Verdict.VIOLATED
