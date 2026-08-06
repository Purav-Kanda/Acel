"""Load temporal contracts and initial state from a plain data file (JSON or YAML).

This is deliberately restricted to *declarative* data: tool names, counts, and
initial state values — never code. It reuses the same safe spec format
:func:`acel.registry.build_contract` already defines for the ``acel replay``
JSON rules file (``{"template": "must_precede", "args": [...]}``), so the same
rules file can drive CI replay checks and a live server's contract set.

Pre/postconditions are **not** representable here on purpose: they need to
evaluate arbitrary logic over state (e.g. ``lambda s: s.get("authenticated")``),
and there is no safe way to deserialize arbitrary logic from a data file
without either an ``eval``-style security hole or a bespoke expression
language. Temporal contracts have no such problem — every template is fully
described by tool names and simple parameters, so building them from a config
file is just constructing objects from validated data, no code execution
involved. Preconditions/postconditions stay Python-only, wired directly to
your own tool implementations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .registry import build_contract
from .temporal import TemporalContract

YAML_SUFFIXES = {".yaml", ".yml"}
JSON_SUFFIXES = {".json"}


class ConfigError(Exception):
    """Raised for a malformed or unreadable rules file."""


def load_rules(path: str | Path) -> dict[str, Any]:
    """Parse a rules file (JSON or YAML, by extension) into a plain dict.

    Expected shape::

        state:
          authenticated: false

        contracts:
          - template: must_precede
            args: [validate_record, delete_record]
          - template: at_most_n_times
            args: [send_payment]
            kwargs: {n: 1}

    Both top-level keys are optional; a file with neither is valid (and useless).
    """
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(f"no such rules file: {path}")

    text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()

    if suffix in JSON_SUFFIXES:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}: invalid JSON — {exc}") from exc
    elif suffix in YAML_SUFFIXES:
        try:
            import yaml
        except ImportError as exc:
            raise ConfigError(
                f"{path}: reading a YAML rules file requires PyYAML. "
                "Install it with: pip install \"acel-core[config]\""
            ) from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: invalid YAML — {exc}") from exc
    else:
        raise ConfigError(
            f"{path}: unrecognized rules file extension {file_path.suffix!r}; "
            f"expected one of {sorted(JSON_SUFFIXES | YAML_SUFFIXES)}"
        )

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(data).__name__}")
    return data


def contracts_from_rules(rules: dict[str, Any]) -> list[TemporalContract]:
    """Build the list of :class:`TemporalContract` declared under ``contracts``."""
    specs = rules.get("contracts", [])
    if not isinstance(specs, list):
        raise ConfigError(f"'contracts' must be a list, got {type(specs).__name__}")
    contracts = []
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise ConfigError(f"contracts[{i}]: expected a mapping, got {type(spec).__name__}")
        try:
            contracts.append(build_contract(spec))
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"contracts[{i}]: {exc}") from exc
    return contracts


def state_from_rules(rules: dict[str, Any]) -> dict[str, Any]:
    """Extract the initial state mapping declared under ``state``.

    ``initial_state`` is accepted too, for backwards compatibility with the
    original ``acel replay`` rules format — ``state`` takes precedence if
    both are present.
    """
    state = rules.get("state", rules.get("initial_state", {}))
    if not isinstance(state, dict):
        raise ConfigError(f"'state' must be a mapping, got {type(state).__name__}")
    return dict(state)


def load_contracts(path: str | Path) -> list[TemporalContract]:
    """Convenience one-shot: parse a rules file straight to a list of contracts."""
    return contracts_from_rules(load_rules(path))
