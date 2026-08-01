"""Violation records and the exception raised when the agent is halted."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Violation:
    """A structured record of a single contract violation.

    In Phase 2/3 this becomes the payload of a signed, hash-chained evidence
    bundle. For the core monitor it already carries everything an auditor
    needs: which contract broke, on which call, and the state at that moment.
    """

    kind: str  # "temporal" | "precondition" | "postcondition"
    spec: str  # human-readable description of the broken contract
    tool: str  # the tool call that triggered the violation
    step: int  # 1-based index of the call within the session
    trace: list[dict[str, Any]] = field(default_factory=list)
    state_snapshot: dict[str, Any] = field(default_factory=dict)
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = None

    @property
    def message(self) -> str:
        return (
            f"{self.kind} violation at step {self.step}: "
            f"call to '{self.tool}' broke contract {self.spec}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "spec": self.spec,
            "tool": self.tool,
            "step": self.step,
            "trace": self.trace,
            "state_snapshot": self.state_snapshot,
            "args": self.args,
            "result": self.result,
        }


class ContractViolation(Exception):
    """Raised to halt the agent the moment a contract is violated."""

    def __init__(self, violation: Violation) -> None:
        self.violation = violation
        super().__init__(violation.message)
