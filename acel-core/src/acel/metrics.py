"""Optional Prometheus-style metrics for a :class:`~acel.session.Session`.

This is opt-in and has zero cost unless you ask for it: pass
``metrics=Metrics()`` to ``Session`` and it tracks call volume, gate
latency, and violation counts (overall and per-contract); leave it out and
none of this code runs. Nothing here talks to a real Prometheus server or
opens a socket — :meth:`Metrics.render_prometheus` just returns the text
exposition format as a string, which you serve however fits your
deployment (a `/metrics` route on whatever web framework fronts your
server, a sidecar, a periodic push job, or just printed to a log). See
:func:`serve_metrics_http` for a zero-dependency stdlib option if you don't
already have an HTTP server to hang a route off of.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from contextlib import contextmanager
from typing import Iterator


def _escape_label_value(value: str) -> str:
    """Escape a string for use inside a Prometheus label value (`{k="v"}`).

    Per the exposition format spec: backslashes and double quotes must be
    backslash-escaped, and newlines become a literal ``\\n``.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    """Thread-safe counters and a latency summary for one Session's activity.

    - ``calls_total`` — every call that reached the gate (whether it passed
      or was blocked).
    - ``violations_total`` — count of violations, broken down by
      ``kind`` (``"temporal"`` / ``"precondition"`` / ``"postcondition"``).
    - ``contract_violations_total`` — count of violations, broken down by
      the exact contract ``spec`` string that broke (e.g.
      ``must_precede(validate, delete)``) — this is what tells you *which*
      rule is actually tripping in production, not just that something did.
    - ``gate_latency_seconds`` — a running count+sum of time spent in the
      precondition+temporal gate per call (the same thing
      ``benchmarks/latency.py`` measures offline, but live, from your own
      traffic) — exposed as a Prometheus ``summary`` (count and sum;
      dividing gives you the average, which is what most dashboards plot).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls_total = 0
        self._violations_by_kind: Counter[str] = Counter()
        self._violations_by_contract: Counter[str] = Counter()
        self._gate_latency_count = 0
        self._gate_latency_sum = 0.0

    def record_call(self) -> None:
        with self._lock:
            self._calls_total += 1

    def record_violation(self, kind: str, contract_spec: str | None = None) -> None:
        with self._lock:
            self._violations_by_kind[kind] += 1
            if contract_spec is not None:
                self._violations_by_contract[contract_spec] += 1

    def record_gate_latency(self, seconds: float) -> None:
        with self._lock:
            self._gate_latency_count += 1
            self._gate_latency_sum += seconds

    @contextmanager
    def time_gate(self) -> Iterator[None]:
        """Context manager: times its body and records it as gate latency."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record_gate_latency(time.perf_counter() - start)

    # --- snapshots (plain data, safe to read without holding the lock) ----

    def snapshot(self) -> dict[str, object]:
        """A plain-data snapshot of every counter, for tests or a JSON endpoint."""
        with self._lock:
            return {
                "calls_total": self._calls_total,
                "violations_by_kind": dict(self._violations_by_kind),
                "violations_by_contract": dict(self._violations_by_contract),
                "gate_latency_count": self._gate_latency_count,
                "gate_latency_sum_seconds": self._gate_latency_sum,
            }

    def render_prometheus(self) -> str:
        """Render everything in Prometheus text exposition format."""
        snap = self.snapshot()
        lines: list[str] = []

        lines.append("# HELP acel_calls_total Total tool calls gated by ACEL.")
        lines.append("# TYPE acel_calls_total counter")
        lines.append(f"acel_calls_total {snap['calls_total']}")

        lines.append("")
        lines.append("# HELP acel_violations_total Total contract violations detected, by kind.")
        lines.append("# TYPE acel_violations_total counter")
        violations_by_kind = snap["violations_by_kind"]
        assert isinstance(violations_by_kind, dict)
        for kind in ("temporal", "precondition", "postcondition"):
            count = violations_by_kind.get(kind, 0)
            lines.append(f'acel_violations_total{{kind="{_escape_label_value(kind)}"}} {count}')

        lines.append("")
        lines.append("# HELP acel_contract_violations_total Violations per contract, identified by its spec string.")
        lines.append("# TYPE acel_contract_violations_total counter")
        violations_by_contract = snap["violations_by_contract"]
        assert isinstance(violations_by_contract, dict)
        for spec, count in sorted(violations_by_contract.items()):
            lines.append(
                f'acel_contract_violations_total{{contract="{_escape_label_value(spec)}"}} {count}'
            )

        lines.append("")
        lines.append("# HELP acel_gate_latency_seconds Time spent in ACEL's precondition+temporal gate per call.")
        lines.append("# TYPE acel_gate_latency_seconds summary")
        lines.append(f"acel_gate_latency_seconds_count {snap['gate_latency_count']}")
        lines.append(f"acel_gate_latency_seconds_sum {snap['gate_latency_sum_seconds']}")

        return "\n".join(lines) + "\n"


def serve_metrics_http(metrics: Metrics, port: int = 9090, path: str = "/metrics"):
    """Start a minimal, zero-dependency HTTP server exposing ``metrics`` at
    ``path`` in Prometheus text format, using only the standard library.

    This is a convenience for the common case of "I don't already have a
    web server running, I just want something Prometheus can scrape" —
    it is *not* meant to replace a real metrics endpoint if your server
    already has an HTTP framework; in that case, just add a route that
    calls ``metrics.render_prometheus()`` yourself and skip this entirely.

    Runs in a background daemon thread; call ``.shutdown()`` on the
    returned ``http.server.HTTPServer`` to stop it. Returns immediately
    (does not block). Imports ``http.server`` lazily, on call, so nothing
    outside this one function pays for it.
    """
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            if self.path != path:
                self.send_response(404)
                self.end_headers()
                return
            body = metrics.render_prometheus().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # quiet by default; the caller can subclass if they want access logs

    server = http.server.HTTPServer(("", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
