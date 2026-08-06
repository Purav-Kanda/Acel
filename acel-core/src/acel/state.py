"""Per-session symbolic state.

Mirrors ToolGate's "trusted world information": a typed key-value store that
preconditions read and postconditions/commits write. The state only evolves
through committed tool results, so hallucinated or unverified data never
corrupts the world representation.
"""

from __future__ import annotations

from typing import Any, Iterator


class StateStore:
    """A simple typed key-value store scoped to one agent session."""

    __slots__ = ("_data",)

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(initial or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update(self, values: dict[str, Any]) -> None:
        self._data.update(values)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def snapshot(self) -> dict[str, Any]:
        """A shallow copy of the current state, for evidence bundles."""
        return dict(self._data)

    def __repr__(self) -> str:
        return f"StateStore({self._data!r})"
