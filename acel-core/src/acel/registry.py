"""Build temporal contracts from plain-data specs (used by the CLI).

A spec is a dict like ``{"template": "must_precede", "args": ["a", "b"]}`` or
``{"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 1}}``. This
lets contracts be declared in a JSON rules file rather than only in Python.
"""

from __future__ import annotations

from typing import Any

from .matchers import matching
from .temporal import (
    TemporalContract,
    at_most_n_times,
    at_most_total,
    cannot_follow_without,
    must_precede,
    mutually_exclusive,
    never_after,
    rate_limit,
    required_before_session_end,
)

TEMPLATES = {
    "must_precede": must_precede,
    "at_most_n_times": at_most_n_times,
    "at_most_total": at_most_total,
    "never_after": never_after,
    "required_before_session_end": required_before_session_end,
    "cannot_follow_without": cannot_follow_without,
    "mutually_exclusive": mutually_exclusive,
    "rate_limit": rate_limit,
}


def _resolve_arg(value: Any) -> Any:
    """Turn a declarative content-matcher dict into a real matcher.

    ``{"tool": "Bash", "matches": "pytest", "field": "command"}`` (``field``
    optional, defaults to ``"command"``) becomes
    ``matching("Bash", "pytest", field="command")`` — see
    :mod:`acel.matchers`. Still just data in, a plain object out: no code
    execution, same safety story as every other config-file value. Any
    non-dict value (the normal case — a plain tool-name string) passes
    through unchanged.
    """
    if not isinstance(value, dict):
        return value
    if "tool" not in value or "matches" not in value:
        raise ValueError(
            "a content-matcher arg must have 'tool' and 'matches' keys "
            f"(optionally 'field'); got keys {sorted(value)}"
        )
    return matching(value["tool"], value["matches"], field=value.get("field", "command"))


def build_contract(spec: dict[str, Any]) -> TemporalContract:
    """Instantiate a temporal contract from a spec dict."""
    template = spec.get("template")
    if template not in TEMPLATES:
        raise ValueError(
            f"unknown template {template!r}; expected one of {sorted(TEMPLATES)}"
        )
    args = [_resolve_arg(a) for a in spec.get("args", [])]
    kwargs = spec.get("kwargs", {})
    return TEMPLATES[template](*args, **kwargs)
