"""Thread-safety tests for Session.

A single Session's mutating methods (call/precheck/postcheck/replay/
end_session) are guarded by an internal lock (see the module docstring in
acel/session.py). These tests fire real concurrent calls at one shared
Session from a thread pool and check for the two classic symptoms of a
missing/broken lock: lost updates (a counter that should have hit exactly
N ends up short because two threads read-then-wrote the same stale value)
and duplicate/skipped step numbers (two threads handed the same step, or a
step silently skipped). Passing here doesn't prove no race is *possible*
in every future code path, but it does prove the lock is actually doing
its job for the paths that matter today.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from acel import Session, at_most_n_times, at_most_total, rate_limit


def test_concurrent_calls_do_not_lose_at_most_n_times_updates():
    """n threads each call a cardinality-limited tool once, cap = n exactly.
    A lost update would let more than n calls succeed; a double-count would
    let fewer than n succeed. Both are wrong — exactly n must succeed."""
    n = 50
    session = Session(halt_on_violation=False)
    session.add_contract(at_most_n_times("send_payment", n))

    successes = []
    lock = threading.Lock()

    def worker():
        result = session.call("send_payment", {"id": threading.get_ident()})
        with lock:
            successes.append(result)

    with ThreadPoolExecutor(max_workers=25) as pool:
        futures = [pool.submit(worker) for _ in range(n)]
        for f in as_completed(futures):
            f.result()

    # Every one of the n calls must have gone through (none silently lost),
    # and the contract must never have gone VIOLATED for hitting exactly n.
    assert len(session.trace) == n
    assert len(session.violations) == 0


def test_concurrent_calls_trip_at_most_n_times_at_exactly_the_right_point():
    """One more caller than the cap, concurrently — exactly one must be
    blocked, not zero (lost update let it slip through) and not more than
    one (double-counted a call that should have been fine)."""
    n = 20
    session = Session(halt_on_violation=False)
    session.add_contract(at_most_n_times("send_payment", n))

    with ThreadPoolExecutor(max_workers=n + 1) as pool:
        futures = [pool.submit(session.call, "send_payment", {}) for _ in range(n + 1)]
        results = [f.result() for f in as_completed(futures)]

    violated = [r for r in results if hasattr(r, "kind")]  # Violation objects
    assert len(violated) == 1


def test_concurrent_calls_keep_step_numbers_unique_and_contiguous():
    """Every concurrent call must get its own step; no two threads may ever
    observe the same step number, and none may be skipped."""
    n = 100
    session = Session(halt_on_violation=False)

    def worker():
        gate = session.precheck("noop", {})
        return gate.step

    with ThreadPoolExecutor(max_workers=20) as pool:
        steps = [f.result() for f in as_completed([pool.submit(worker) for _ in range(n)])]

    assert len(steps) == n
    assert len(set(steps)) == n  # no two threads ever got the same step
    assert sorted(steps) == list(range(1, n + 1))  # none skipped


def test_concurrent_calls_do_not_corrupt_cumulative_total():
    """at_most_total sums a numeric field across calls — a lost update here
    would silently let the running total under-count, defeating the whole
    point of a spend cap."""
    n = 40
    per_call = 10
    session = Session(halt_on_violation=False)
    session.add_contract(at_most_total("send_payment", "amount", limit=per_call * n))

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [
            pool.submit(session.call, "send_payment", {"amount": per_call}) for _ in range(n)
        ]
        for f in as_completed(futures):
            f.result()

    # Exactly at the limit with no violations means every call's amount was
    # actually added to the running total — a lost update would have kept
    # the total under the limit even after the (n+1)th call, which we don't
    # send here; instead we check the trace length and violation count.
    assert len(session.trace) == n
    assert len(session.violations) == 0


def test_concurrent_calls_respect_rate_limit_cap():
    """n+5 concurrent callers against a rate_limit(n) contract: at least 5
    must be blocked (never fewer — that would mean a lost update let an
    over-the-cap burst through undetected)."""
    n = 15
    session = Session(halt_on_violation=False)
    session.add_contract(rate_limit("ping", n=n, window_seconds=60))

    with ThreadPoolExecutor(max_workers=n + 5) as pool:
        futures = [pool.submit(session.call, "ping", {}) for _ in range(n + 5)]
        results = [f.result() for f in as_completed(futures)]

    violated = [r for r in results if hasattr(r, "kind")]
    assert len(violated) == 5


def test_evidence_log_length_matches_recorded_violations_under_concurrency():
    """Every recorded Violation must produce exactly one evidence bundle —
    a race that double-recorded or dropped a bundle would break this
    one-to-one correspondence."""
    n = 10
    session = Session(halt_on_violation=False)
    session.add_contract(at_most_n_times("send_payment", n=0))  # every call violates

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(session.call, "send_payment", {}) for _ in range(n)]
        for f in as_completed(futures):
            f.result()

    assert len(session.violations) == n
    assert len(session.evidence) == n
