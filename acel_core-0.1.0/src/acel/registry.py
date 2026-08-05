"""Build temporal contracts from plain-data specs (used by the CLI).

A spec is a dict like ``{"template": "must_precede", "args": ["a", "b"]}`` or
``{"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 1}}``. This
lets contracts be declared in a JSON rules file rather than only in Python.
"""

from __future__ import annotations

from typing import Any

from .temporal import (
    TemporalContract,
    at_most_n_times,
    cannot_follow_without,
    must_precede,
    mutually_exclusive,
    never_after,
    required_before_session_end,
)

TEMPLATES = {
    "must_precede": must_precede,
    "at_most_n_times": at_most_n_times,
    "never_after": never_after,
    "required_before_session_end": required_before_session_end,
    "cannot_follow_without": cannot_follow_without,
    "mutually_exclusive": mutually_exclusive,
}


def build_contract(spec: dict[str, Any]) -> TemporalContract:
    """Instantiate a temporal contract from a spec dict."""
    template = spec.get("template")
    if template not in TEMPLATES:
        raise ValueError(
            f"unknown template {template!r}; expected one of {sorted(TEMPLATES)}"
        )
    args = spec.get("args", [])
    kwargs = spec.get("kwargs", {})
    return TEMPLATES[template](*args, **kwargs)
