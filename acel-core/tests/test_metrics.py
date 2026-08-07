"""Tests for the optional Prometheus-style Metrics collector."""

from __future__ import annotations

import urllib.error
import urllib.request

from acel import Metrics, Session, must_precede, serve_metrics_http


def test_calls_total_starts_at_zero():
    m = Metrics()
    assert m.snapshot()["calls_total"] == 0


def test_record_call_increments_calls_total():
    m = Metrics()
    m.record_call()
    m.record_call()
    assert m.snapshot()["calls_total"] == 2


def test_record_violation_tracks_by_kind():
    m = Metrics()
    m.record_violation("temporal")
    m.record_violation("temporal")
    m.record_violation("precondition")
    snap = m.snapshot()
    assert snap["violations_by_kind"] == {"temporal": 2, "precondition": 1}


def test_record_violation_tracks_by_contract_spec():
    m = Metrics()
    m.record_violation("temporal", "must_precede(a, b)")
    m.record_violation("temporal", "must_precede(a, b)")
    m.record_violation("temporal", "never_after(c, d)")
    snap = m.snapshot()
    assert snap["violations_by_contract"] == {
        "must_precede(a, b)": 2,
        "never_after(c, d)": 1,
    }


def test_record_violation_without_contract_spec_only_updates_kind():
    m = Metrics()
    m.record_violation("precondition")  # no contract spec, e.g. a tool-level precondition
    snap = m.snapshot()
    assert snap["violations_by_kind"] == {"precondition": 1}
    assert snap["violations_by_contract"] == {}


def test_record_gate_latency_accumulates_count_and_sum():
    m = Metrics()
    m.record_gate_latency(0.001)
    m.record_gate_latency(0.002)
    snap = m.snapshot()
    assert snap["gate_latency_count"] == 2
    assert snap["gate_latency_sum_seconds"] == 0.003


def test_time_gate_context_manager_records_a_sample():
    m = Metrics()
    with m.time_gate():
        pass
    snap = m.snapshot()
    assert snap["gate_latency_count"] == 1
    assert snap["gate_latency_sum_seconds"] >= 0.0


# --- Prometheus text format ------------------------------------------------


def test_render_prometheus_includes_all_metric_families():
    m = Metrics()
    text = m.render_prometheus()
    assert "# TYPE acel_calls_total counter" in text
    assert "# TYPE acel_violations_total counter" in text
    assert "# TYPE acel_contract_violations_total counter" in text
    assert "# TYPE acel_gate_latency_seconds summary" in text
    assert "acel_calls_total 0" in text


def test_render_prometheus_reflects_recorded_values():
    m = Metrics()
    m.record_call()
    m.record_call()
    m.record_violation("temporal", "must_precede(a, b)")
    m.record_gate_latency(0.5)
    text = m.render_prometheus()
    assert "acel_calls_total 2" in text
    assert 'acel_violations_total{kind="temporal"} 1' in text
    assert 'acel_contract_violations_total{contract="must_precede(a, b)"} 1' in text
    assert "acel_gate_latency_seconds_count 1" in text
    assert "acel_gate_latency_seconds_sum 0.5" in text


def test_render_prometheus_escapes_quotes_and_backslashes_in_contract_labels():
    m = Metrics()
    m.record_violation("temporal", 'weird "spec" with \\backslash')
    text = m.render_prometheus()
    assert '\\"spec\\"' in text
    assert "\\\\backslash" in text


def test_render_prometheus_lists_all_three_violation_kinds_even_at_zero():
    # Dashboards querying a specific kind label shouldn't get "no data" just
    # because that kind hasn't happened yet.
    m = Metrics()
    text = m.render_prometheus()
    assert 'acel_violations_total{kind="temporal"} 0' in text
    assert 'acel_violations_total{kind="precondition"} 0' in text
    assert 'acel_violations_total{kind="postcondition"} 0' in text


# --- Session wiring ----------------------------------------------------


def test_session_without_metrics_has_none_attribute():
    session = Session()
    assert session.metrics is None


def test_session_call_records_call_and_gate_latency():
    m = Metrics()
    session = Session(metrics=m, halt_on_violation=False)
    session.call("read_data", {})
    snap = m.snapshot()
    assert snap["calls_total"] == 1
    assert snap["gate_latency_count"] == 1


def test_session_call_records_violation_by_kind_and_contract():
    m = Metrics()
    session = Session(metrics=m, halt_on_violation=False)
    session.add_contract(must_precede("validate", "delete"))
    session.call("delete", {})
    snap = m.snapshot()
    assert snap["violations_by_kind"] == {"temporal": 1}
    assert snap["violations_by_contract"] == {"must_precede(validate, delete)": 1}


def test_session_precheck_records_call_and_latency():
    m = Metrics()
    session = Session(metrics=m)
    session.precheck("read_data", {})
    snap = m.snapshot()
    assert snap["calls_total"] == 1
    assert snap["gate_latency_count"] == 1


def test_session_multiple_calls_accumulate_metrics():
    m = Metrics()
    session = Session(metrics=m, halt_on_violation=False)
    session.add_contract(must_precede("validate", "delete"))
    session.call("validate", {})
    session.call("delete", {})
    session.call("delete", {})  # fine, no cardinality cap here
    snap = m.snapshot()
    assert snap["calls_total"] == 3
    assert snap["gate_latency_count"] == 3
    assert snap["violations_by_kind"] == {}


# --- serve_metrics_http --------------------------------------------------


def test_serve_metrics_http_exposes_metrics_over_real_http():
    m = Metrics()
    m.record_call()
    server = serve_metrics_http(m, port=0)  # port 0 -> OS picks a free port
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
            body = resp.read().decode("utf-8")
            content_type = resp.headers.get("Content-Type")
        assert "acel_calls_total 1" in body
        assert content_type is not None and "text/plain" in content_type
    finally:
        server.shutdown()
        server.server_close()


def test_serve_metrics_http_404s_on_other_paths():
    m = Metrics()
    server = serve_metrics_http(m, port=0)
    try:
        port = server.server_address[1]
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
            assert False, "expected an HTTPError"
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()
