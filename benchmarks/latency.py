"""Measure ACEL's added latency per tool call.

Compares a bare no-op tool call against the same call gated through a
Session with 1 / 10 / 50 concurrently active temporal contracts, none of
which are ever violated (this measures the steady-state gating cost, not the
halt path). Reports mean / p95 / p99 overhead in milliseconds.

Run: python benchmarks/latency.py
"""

from __future__ import annotations

import statistics
import time

from acel import Session, must_precede

ITERATIONS = 20_000
WARMUP = 1_000


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * pct
    f, c = int(k), min(int(k) + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def _noop_tool(**kwargs) -> dict:
    return {"ok": True}


def time_baseline(iterations: int) -> list[float]:
    """The cost of just calling the tool function directly — no ACEL at all."""
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        _noop_tool(x=1)
        samples.append((time.perf_counter() - start) * 1000)
    return samples


def time_with_acel(iterations: int, n_contracts: int) -> list[float]:
    """The cost of the same call, gated through a Session with N active contracts.

    The contracts reference tool names other than the one being called, so
    every contract is exercised on every call (via on_event) without ever
    being satisfied or violated — the realistic steady-state cost of having
    N rules registered in a live session.
    """
    session = Session()
    for i in range(n_contracts):
        session.add_contract(must_precede(f"earlier_{i}", f"later_{i}"))

    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        session.call("noop_tool", {"x": 1}, run=_noop_tool)
        samples.append((time.perf_counter() - start) * 1000)
    return samples


def summarize(label: str, samples: list[float]) -> dict[str, float]:
    warm = samples[WARMUP:] if len(samples) > WARMUP else samples
    warm_sorted = sorted(warm)
    stats = {
        "mean_ms": statistics.mean(warm),
        "p95_ms": _percentile(warm_sorted, 0.95),
        "p99_ms": _percentile(warm_sorted, 0.99),
    }
    print(f"{label:<28} mean={stats['mean_ms']:.4f}ms  p95={stats['p95_ms']:.4f}ms  p99={stats['p99_ms']:.4f}ms")
    return stats


def main() -> None:
    print(f"ACEL latency benchmark — {ITERATIONS:,} iterations ({WARMUP:,} warmup, discarded)\n")

    baseline = summarize("baseline (no ACEL)", time_baseline(ITERATIONS))

    print()
    for n in (1, 10, 50):
        acel_stats = summarize(f"ACEL, {n} contract(s)", time_with_acel(ITERATIONS, n))
        overhead_p95 = acel_stats["p95_ms"] - baseline["p95_ms"]
        print(f"{'':<28} -> added p95 overhead: {overhead_p95:.4f}ms")
        print()


if __name__ == "__main__":
    main()
