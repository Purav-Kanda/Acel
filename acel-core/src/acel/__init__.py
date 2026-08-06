"""ACEL — Agent Contract Enforcement Layer (core monitor).

Runtime verification of AI agent tool calls: declare temporal ordering
contracts and Hoare-style pre/postconditions in plain Python, and have them
enforced live against the stream of tool calls.

This package is the MCP-independent core (Phase 1). The MCP proxy and the
signed, hash-chained evidence bundles build on top of it in later phases.
"""

from __future__ import annotations

from .conditions import Check, postcondition, precondition
from .events import ToolCallEvent
from .evidence import EvidenceBundle, EvidenceLog, ed25519_signer, redact_violation
from .session import Gate, Session, ToolSpec
from .state import StateStore
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
from .verdict import Verdict
from .violations import ContractViolation, Violation

__version__ = "0.1.7"

__all__ = [
    "__version__",
    # core
    "Session",
    "ToolSpec",
    "Gate",
    "StateStore",
    "ToolCallEvent",
    "Verdict",
    "Violation",
    "ContractViolation",
    # evidence
    "EvidenceLog",
    "EvidenceBundle",
    "ed25519_signer",
    "redact_violation",
    # conditions
    "precondition",
    "postcondition",
    "Check",
    # temporal templates
    "TemporalContract",
    "must_precede",
    "at_most_n_times",
    "at_most_total",
    "never_after",
    "required_before_session_end",
    "cannot_follow_without",
    "mutually_exclusive",
    "rate_limit",
]
