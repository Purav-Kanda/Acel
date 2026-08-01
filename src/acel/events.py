"""The unit of observation: a single tool call in an agent session."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ToolCallEvent:
    """One tool call observed by the monitor.

    The monitor is transport-agnostic: it consumes a stream of these events,
    whether they originate from a live MCP proxy (Phase 2) or a recorded
    trace being replayed (``Session.replay``).
    """

    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)
    result: Any = None
    ts: float = field(default_factory=time.time)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ToolCallEvent":
        """Build an event from a plain dict (e.g. a JSON trace row)."""
        return cls(
            tool=record["tool"],
            args=record.get("args", {}),
            result=record.get("result"),
            ts=record.get("ts", time.time()),
        )
