"""A labeled dataset of synthetic tool-call traces for measuring ACEL's
correctness: does the monitor catch every violation (recall) and only real
violations (precision)?

Each Case wires one or more temporal contracts to a trace and states whether
that trace *should* trigger a violation. Covers all 8 templates with both
passing and violating sequences, plus a few edge cases and multi-contract
combinations — 67 cases total.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    template: str
    contracts: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    expect_violation: bool


def _t(tool: str) -> dict[str, Any]:
    return {"tool": tool}


def _ta(tool: str, **args: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args}


CASES: list[Case] = [
    # --- must_precede(validate, delete) ---------------------------------
    Case("must_precede_01_valid_simple", "must_precede",
         [{"template": "must_precede", "args": ["validate", "delete"]}],
         [_t("validate"), _t("delete")], False),
    Case("must_precede_02_valid_repeated", "must_precede",
         [{"template": "must_precede", "args": ["validate", "delete"]}],
         [_t("validate"), _t("delete"), _t("delete")], False),
    Case("must_precede_03_valid_only_earlier", "must_precede",
         [{"template": "must_precede", "args": ["validate", "delete"]}],
         [_t("validate")], False),
    Case("must_precede_04_violation_missing_earlier", "must_precede",
         [{"template": "must_precede", "args": ["validate", "delete"]}],
         [_t("delete")], True),
    Case("must_precede_05_violation_wrong_order", "must_precede",
         [{"template": "must_precede", "args": ["validate", "delete"]}],
         [_t("delete"), _t("validate")], True),
    Case("must_precede_06_valid_with_noise", "must_precede",
         [{"template": "must_precede", "args": ["validate", "delete"]}],
         [_t("validate"), _t("other"), _t("delete")], False),
    Case("must_precede_07_valid_empty_trace", "must_precede",
         [{"template": "must_precede", "args": ["validate", "delete"]}],
         [], False),
    Case("must_precede_08_violation_repeated_bad", "must_precede",
         [{"template": "must_precede", "args": ["validate", "delete"]}],
         [_t("delete"), _t("delete")], True),

    # --- at_most_n_times(send_payment, n=2) -----------------------------
    Case("at_most_n_01_valid_at_limit", "at_most_n_times",
         [{"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 2}}],
         [_t("pay"), _t("pay")], False),
    Case("at_most_n_02_valid_under_limit", "at_most_n_times",
         [{"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 2}}],
         [_t("pay")], False),
    Case("at_most_n_03_valid_zero_calls", "at_most_n_times",
         [{"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 2}}],
         [], False),
    Case("at_most_n_04_violation_over_limit", "at_most_n_times",
         [{"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 2}}],
         [_t("pay"), _t("pay"), _t("pay")], True),
    Case("at_most_n_05_violation_well_over_limit", "at_most_n_times",
         [{"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 2}}],
         [_t("pay"), _t("pay"), _t("pay"), _t("pay")], True),
    Case("at_most_n_06_violation_zero_allowed", "at_most_n_times",
         [{"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 0}}],
         [_t("pay")], True),
    Case("at_most_n_07_valid_zero_allowed_zero_calls", "at_most_n_times",
         [{"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 0}}],
         [], False),
    Case("at_most_n_08_valid_interleaved", "at_most_n_times",
         [{"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 2}}],
         [_t("pay"), _t("other"), _t("pay")], False),

    # --- at_most_total(send_payment, amount, limit=100) -------------------
    Case("at_most_total_01_valid_under_limit", "at_most_total",
         [{"template": "at_most_total", "args": ["send_payment", "amount"], "kwargs": {"limit": 100}}],
         [_ta("send_payment", amount=40), _ta("send_payment", amount=30)], False),
    Case("at_most_total_02_valid_exactly_at_limit", "at_most_total",
         [{"template": "at_most_total", "args": ["send_payment", "amount"], "kwargs": {"limit": 100}}],
         [_ta("send_payment", amount=60), _ta("send_payment", amount=40)], False),
    Case("at_most_total_03_violation_over_limit", "at_most_total",
         [{"template": "at_most_total", "args": ["send_payment", "amount"], "kwargs": {"limit": 100}}],
         [_ta("send_payment", amount=60), _ta("send_payment", amount=41)], True),
    Case("at_most_total_04_violation_single_call_over_limit", "at_most_total",
         [{"template": "at_most_total", "args": ["send_payment", "amount"], "kwargs": {"limit": 100}}],
         [_ta("send_payment", amount=150)], True),
    Case("at_most_total_05_valid_ignores_other_tools", "at_most_total",
         [{"template": "at_most_total", "args": ["send_payment", "amount"], "kwargs": {"limit": 100}}],
         [_ta("other_tool", amount=99999), _ta("send_payment", amount=10)], False),
    Case("at_most_total_06_violation_missing_field", "at_most_total",
         [{"template": "at_most_total", "args": ["send_payment", "amount"], "kwargs": {"limit": 100}}],
         [_t("send_payment")], True),
    Case("at_most_total_07_violation_non_numeric_field", "at_most_total",
         [{"template": "at_most_total", "args": ["send_payment", "amount"], "kwargs": {"limit": 100}}],
         [_ta("send_payment", amount="a lot")], True),
    Case("at_most_total_08_valid_zero_calls", "at_most_total",
         [{"template": "at_most_total", "args": ["send_payment", "amount"], "kwargs": {"limit": 100}}],
         [], False),

    # --- never_after(read, close) ----------------------------------------
    Case("never_after_01_valid_all_before", "never_after",
         [{"template": "never_after", "args": ["read", "close"]}],
         [_t("read"), _t("read"), _t("close")], False),
    Case("never_after_02_valid_marker_only", "never_after",
         [{"template": "never_after", "args": ["read", "close"]}],
         [_t("close")], False),
    Case("never_after_03_valid_empty", "never_after",
         [{"template": "never_after", "args": ["read", "close"]}],
         [], False),
    Case("never_after_04_violation_simple", "never_after",
         [{"template": "never_after", "args": ["read", "close"]}],
         [_t("close"), _t("read")], True),
    Case("never_after_05_violation_with_noise", "never_after",
         [{"template": "never_after", "args": ["read", "close"]}],
         [_t("read"), _t("close"), _t("read")], True),
    Case("never_after_06_valid_marker_then_other", "never_after",
         [{"template": "never_after", "args": ["read", "close"]}],
         [_t("read"), _t("close"), _t("other")], False),
    Case("never_after_07_violation_far_apart", "never_after",
         [{"template": "never_after", "args": ["read", "close"]}],
         [_t("close"), _t("other"), _t("other"), _t("read")], True),
    Case("never_after_08_valid_many_before_marker", "never_after",
         [{"template": "never_after", "args": ["read", "close"]}],
         [_t("read"), _t("read"), _t("read"), _t("close")], False),

    # --- required_before_session_end(authenticate) ------------------------
    Case("required_01_valid_present", "required_before_session_end",
         [{"template": "required_before_session_end", "args": ["authenticate"]}],
         [_t("authenticate")], False),
    Case("required_02_violation_missing", "required_before_session_end",
         [{"template": "required_before_session_end", "args": ["authenticate"]}],
         [_t("other")], True),
    Case("required_03_valid_last_event", "required_before_session_end",
         [{"template": "required_before_session_end", "args": ["authenticate"]}],
         [_t("other"), _t("authenticate")], False),
    Case("required_04_violation_empty_trace", "required_before_session_end",
         [{"template": "required_before_session_end", "args": ["authenticate"]}],
         [], True),
    Case("required_05_valid_with_trailing_calls", "required_before_session_end",
         [{"template": "required_before_session_end", "args": ["authenticate"]}],
         [_t("authenticate"), _t("other"), _t("other")], False),
    Case("required_06_violation_many_other_calls", "required_before_session_end",
         [{"template": "required_before_session_end", "args": ["authenticate"]}],
         [_t("other"), _t("other"), _t("other")], True),
    Case("required_07_valid_repeated", "required_before_session_end",
         [{"template": "required_before_session_end", "args": ["authenticate"]}],
         [_t("authenticate"), _t("authenticate")], False),
    Case("required_08_violation_wrong_tool_names", "required_before_session_end",
         [{"template": "required_before_session_end", "args": ["authenticate"]}],
         [_t("login"), _t("signin")], True),

    # --- cannot_follow_without(delete, backup) -----------------------------
    Case("cfw_01_valid_simple", "cannot_follow_without",
         [{"template": "cannot_follow_without", "args": ["delete", "backup"]}],
         [_t("backup"), _t("delete")], False),
    Case("cfw_02_violation_no_prereq", "cannot_follow_without",
         [{"template": "cannot_follow_without", "args": ["delete", "backup"]}],
         [_t("delete")], True),
    Case("cfw_03_valid_repeated_both", "cannot_follow_without",
         [{"template": "cannot_follow_without", "args": ["delete", "backup"]}],
         [_t("backup"), _t("backup"), _t("delete"), _t("delete")], False),
    Case("cfw_04_violation_wrong_order", "cannot_follow_without",
         [{"template": "cannot_follow_without", "args": ["delete", "backup"]}],
         [_t("delete"), _t("backup")], True),
    Case("cfw_05_valid_action_never_happens", "cannot_follow_without",
         [{"template": "cannot_follow_without", "args": ["delete", "backup"]}],
         [], False),
    Case("cfw_06_violation_with_noise", "cannot_follow_without",
         [{"template": "cannot_follow_without", "args": ["delete", "backup"]}],
         [_t("other"), _t("delete")], True),
    Case("cfw_07_valid_prereq_then_noise_then_action", "cannot_follow_without",
         [{"template": "cannot_follow_without", "args": ["delete", "backup"]}],
         [_t("backup"), _t("other"), _t("delete")], False),
    Case("cfw_08_violation_action_first_then_prereq", "cannot_follow_without",
         [{"template": "cannot_follow_without", "args": ["delete", "backup"]}],
         [_t("delete"), _t("other"), _t("backup")], True),

    # --- mutually_exclusive(prod_write, test_write) -------------------------
    Case("mutex_01_valid_only_a", "mutually_exclusive",
         [{"template": "mutually_exclusive", "args": ["prod_write", "test_write"]}],
         [_t("prod_write")], False),
    Case("mutex_02_valid_only_b", "mutually_exclusive",
         [{"template": "mutually_exclusive", "args": ["prod_write", "test_write"]}],
         [_t("test_write")], False),
    Case("mutex_03_valid_neither", "mutually_exclusive",
         [{"template": "mutually_exclusive", "args": ["prod_write", "test_write"]}],
         [_t("other")], False),
    Case("mutex_04_violation_a_then_b", "mutually_exclusive",
         [{"template": "mutually_exclusive", "args": ["prod_write", "test_write"]}],
         [_t("prod_write"), _t("test_write")], True),
    Case("mutex_05_violation_b_then_a", "mutually_exclusive",
         [{"template": "mutually_exclusive", "args": ["prod_write", "test_write"]}],
         [_t("test_write"), _t("prod_write")], True),
    Case("mutex_06_valid_repeated_a_only", "mutually_exclusive",
         [{"template": "mutually_exclusive", "args": ["prod_write", "test_write"]}],
         [_t("prod_write"), _t("prod_write"), _t("prod_write")], False),
    Case("mutex_07_violation_with_noise", "mutually_exclusive",
         [{"template": "mutually_exclusive", "args": ["prod_write", "test_write"]}],
         [_t("prod_write"), _t("other"), _t("test_write")], True),
    Case("mutex_08_valid_empty", "mutually_exclusive",
         [{"template": "mutually_exclusive", "args": ["prod_write", "test_write"]}],
         [], False),

    # --- rate_limit(send_payment, n=2, window_seconds=60) ------------------
    # These run as a fast burst with no real delay between calls, so every
    # call in a case's trace lands inside the same window by construction —
    # that's enough to exercise the cardinality check (does N+1 in a window
    # trip it). Window-*aging* behavior (old calls falling out of the window
    # over elapsed time) needs a controllable fake clock to test
    # deterministically without real sleeping — covered in test_templates.py
    # instead, not here.
    Case("rate_limit_01_valid_at_limit", "rate_limit",
         [{"template": "rate_limit", "args": ["send_payment"], "kwargs": {"n": 2, "window_seconds": 60}}],
         [_t("send_payment"), _t("send_payment")], False),
    Case("rate_limit_02_valid_under_limit", "rate_limit",
         [{"template": "rate_limit", "args": ["send_payment"], "kwargs": {"n": 2, "window_seconds": 60}}],
         [_t("send_payment")], False),
    Case("rate_limit_03_valid_zero_calls", "rate_limit",
         [{"template": "rate_limit", "args": ["send_payment"], "kwargs": {"n": 2, "window_seconds": 60}}],
         [], False),
    Case("rate_limit_04_violation_over_limit", "rate_limit",
         [{"template": "rate_limit", "args": ["send_payment"], "kwargs": {"n": 2, "window_seconds": 60}}],
         [_t("send_payment"), _t("send_payment"), _t("send_payment")], True),
    Case("rate_limit_05_violation_well_over_limit", "rate_limit",
         [{"template": "rate_limit", "args": ["send_payment"], "kwargs": {"n": 2, "window_seconds": 60}}],
         [_t("send_payment"), _t("send_payment"), _t("send_payment"), _t("send_payment")], True),
    Case("rate_limit_06_valid_ignores_other_tools", "rate_limit",
         [{"template": "rate_limit", "args": ["send_payment"], "kwargs": {"n": 1, "window_seconds": 60}}],
         [_t("other"), _t("other"), _t("send_payment")], False),
    Case("rate_limit_07_valid_interleaved_under_limit", "rate_limit",
         [{"template": "rate_limit", "args": ["send_payment"], "kwargs": {"n": 2, "window_seconds": 60}}],
         [_t("send_payment"), _t("other"), _t("send_payment")], False),
    Case("rate_limit_08_violation_interleaved_over_limit", "rate_limit",
         [{"template": "rate_limit", "args": ["send_payment"], "kwargs": {"n": 1, "window_seconds": 60}}],
         [_t("send_payment"), _t("other"), _t("send_payment")], True),

    # --- multi-contract combinations ---------------------------------------
    Case("combo_01_both_satisfied", "combo",
         [
             {"template": "must_precede", "args": ["validate", "delete"]},
             {"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 1}},
         ],
         [_t("validate"), _t("delete"), _t("pay")], False),
    Case("combo_02_one_violated", "combo",
         [
             {"template": "must_precede", "args": ["validate", "delete"]},
             {"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 1}},
         ],
         [_t("validate"), _t("delete"), _t("pay"), _t("pay")], True),
    Case("combo_03_both_violated", "combo",
         [
             {"template": "must_precede", "args": ["validate", "delete"]},
             {"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 1}},
         ],
         [_t("delete"), _t("pay"), _t("pay")], True),
]
