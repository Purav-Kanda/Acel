"""Thin, dependency-free helpers for wiring a Session into agent frameworks
other than MCP.

``Session.call()`` is already framework-agnostic — it takes a tool name, a
dict of arguments, and a callable to run, and returns the result (or raises/
returns a ``Violation`` on a blocked call). Nothing about it is MCP-specific;
the MCP proxy (``acel.mcp_middleware``) is just one particular caller of it.
These helpers exist purely to save the boilerplate of matching a specific
framework's own tool-call shape (LangChain's keyword-argument tool
functions, OpenAI's JSON-encoded ``tool_calls``) onto that same generic
``Session.call()`` — they don't add any enforcement logic of their own.

No framework is imported here. ``guard()`` works with any plain callable
(LangChain tool functions included, since they're just callables taking
keyword arguments); ``guard_openai_tool_call()`` only needs the standard
library's ``json``. See ``examples/langchain_agent_example.py`` and
``examples/openai_function_calling_example.py`` for complete, runnable
demonstrations of both.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .session import Session

ToolFunc = Callable[..., Any]


def guard(session: Session, tool: str, func: ToolFunc) -> ToolFunc:
    """Wrap a plain keyword-argument callable so every invocation is gated
    through ``session.call()`` first.

    Returns a new callable with the same keyword-argument calling
    convention as ``func`` — drop it in wherever ``func`` was used directly
    (a LangChain tool's ``func=``, a plain dict of ``{name: callable}``
    dispatch table, anything that just calls ``the_tool(**kwargs)``). The
    keyword arguments passed in become the tool's ``args`` dict for
    contract/state-precondition purposes, exactly as if you'd called
    ``session.call(tool, kwargs, run=...)`` yourself.

    In enforce mode, a blocked call raises :class:`~acel.violations.ContractViolation`
    from inside the wrapped callable (propagating out through whatever
    invoked it, e.g. a LangChain agent executor) unless the session was
    built with ``halt_on_violation=False``, in which case it returns the
    :class:`~acel.violations.Violation` instead of the tool's real result —
    check the return type if you use that mode.
    """

    def wrapped(**kwargs: Any) -> Any:
        return session.call(tool, kwargs, run=lambda **a: func(**a))

    wrapped.__name__ = getattr(func, "__name__", tool)
    wrapped.__doc__ = getattr(func, "__doc__", None)
    return wrapped


def guard_openai_tool_call(
    session: Session,
    tool_call: dict[str, Any],
    func: ToolFunc,
) -> Any:
    """Gate one OpenAI-style ``tool_calls[i]`` entry through ``session``,
    then run ``func`` if it passes.

    ``tool_call`` is the raw shape the OpenAI-compatible chat completions
    API returns: ``{"function": {"name": "...", "arguments": "<json string
    or already-parsed dict>"}}`` (the ``id``/``type`` fields, if present,
    are ignored — only ``function`` is read). Returns ``func``'s result on
    success. Same enforce/shadow-mode return-value behavior as
    :func:`guard` — see its docstring.
    """
    function = tool_call["function"]
    name = function["name"]
    raw_args = function["arguments"]
    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
    return session.call(name, args, run=lambda **a: func(**a))
