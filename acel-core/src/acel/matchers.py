"""Content-aware tool matchers for temporal contracts.

Every temporal template (:mod:`acel.temporal`) takes a tool identifier —
``must_precede(earlier, later)``, ``at_most_n_times(tool, n)``, and so on.
Passing a plain string there matches on tool *name* only, exactly as before
(fully backward compatible). This module adds the other option: a matcher
that also inspects the call's *arguments*, so a rule can distinguish, say,
a ``Bash`` call running ``pytest`` from a ``Bash`` call running
``git commit`` — both the same tool name, different intent.

This is what makes rules like "a `git commit` must be preceded by a test
run" possible, which plain tool-name matching cannot express (see the
README's "Guarding Claude Code itself" section for the full example).

**Still no arbitrary code from config files.** :class:`ContentMatch` is a
plain Python object with a compiled regex and a field name — when built via
:func:`matching`, there is no ``eval``, no arbitrary predicate, nothing
resembling code execution. A YAML/JSON rules file can declare one exactly
as safely as it declares a plain tool name: ``{"tool": "Bash", "matches":
"pytest", "field": "command"}`` (see :mod:`acel.config`/:mod:`acel.registry`
for the declarative form). If you need a genuinely arbitrary predicate over
args, build a :class:`ContentMatch` directly in Python instead — that part
necessarily can't come from a data file, for the same reason
preconditions/postconditions can't.
"""

from __future__ import annotations

import re
from typing import Any


class ContentMatch:
    """A tool-name + argument-content matcher, usable anywhere a temporal
    template currently accepts a plain tool-name string.

    Calling it with ``(tool, args)`` (the same signature every temporal
    template's ``_step`` already receives) returns whether this call
    counts as a match — the tool name matches *and* the predicate over its
    arguments holds.
    """

    def __init__(self, tool: str, predicate: "Any", *, label: str | None = None) -> None:
        self.tool = tool
        self.predicate = predicate
        self.label = label or f"{tool}(matching custom predicate)"

    def __call__(self, tool: str, args: dict[str, Any]) -> bool:
        if tool != self.tool:
            return False
        try:
            return bool(self.predicate(args))
        except Exception:
            return False  # fail closed: an errored predicate is not a match

    def __str__(self) -> str:
        return self.label

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<ContentMatch {self.label}>"


def matching(tool: str, pattern: str, *, field: str = "command") -> ContentMatch:
    """Match a ``tool`` call whose ``args[field]`` (stringified) contains a
    regex match for ``pattern`` (case-insensitive).

    ``field`` defaults to ``"command"``, the argument Claude Code's ``Bash``
    and ``PowerShell`` tools use — the common case for "was this shell
    command a test run / a git commit / etc." Pass a different ``field``
    for other tools, e.g. ``matching("Write", r"\\.env$", field="file_path")``
    to match writes to a ``.env`` file.

    A call missing ``field`` entirely, or where it isn't a string-like
    value, simply doesn't match (not an error) — same "absent means no"
    behavior as a missing tool-name match.
    """
    compiled = re.compile(pattern, re.IGNORECASE)

    def predicate(args: dict[str, Any]) -> bool:
        value = args.get(field)
        if value is None:
            return False
        return bool(compiled.search(str(value)))

    return ContentMatch(tool, predicate, label=f"{tool}({field}~/{pattern}/)")
