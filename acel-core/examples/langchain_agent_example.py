"""ACEL guarding LangChain tools — no MCP involved.

Proves the "works with any Python agent loop, not just MCP" claim
concretely: this wraps two ordinary LangChain ``@tool``-decorated functions
with ``acel.adapters.guard`` so every invocation goes through the same
Session gate the MCP proxy uses, then shows a call sequence that trips a
``must_precede`` contract — the delete is blocked *before* it runs, exactly
like the MCP examples, just with LangChain's own tool-calling convention
instead of an MCP client.

Requires the optional ``langchain-core`` dependency:

    pip install "acel-core[langchain]"

Run directly for a self-contained demo (no LLM/API key needed — this
invokes the guarded tools directly to demonstrate the gate, the same way
`toy_server.py` demonstrates the MCP proxy without a live agent attached):

    python examples/langchain_agent_example.py

Wiring these into a real LangChain agent (an ``AgentExecutor`` with an
LLM actually deciding which tool to call) is no different from wiring in
any other LangChain tool — pass the guarded tools from ``build_tools()``
below wherever you'd normally pass the unguarded ones.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool, tool

from acel import Session, must_precede
from acel.adapters import guard
from acel.violations import ContractViolation


def build_session() -> Session:
    session = Session()
    session.add_contract(must_precede("validate_record", "delete_record"))
    return session


@tool
def validate_record(record_id: str) -> str:
    """Validate a record exists and is safe to delete."""
    return f"record {record_id} validated"


@tool
def delete_record(record_id: str) -> str:
    """Permanently delete a record. Destructive — must be validated first."""
    return f"record {record_id} deleted"


def build_tools(session: Session) -> list[StructuredTool]:
    """Return LangChain tools identical in shape to the ones above, except
    every invocation is gated through ``session`` first via
    :func:`acel.adapters.guard`.

    ``guard`` wraps the tool's underlying function, not the ``@tool``
    decorator itself — so the guarded tool still has the same name,
    description, and calling convention a LangChain agent expects; ACEL's
    gate is invisible to the agent until it actually breaks a rule.
    """
    return [
        StructuredTool.from_function(
            func=guard(session, "validate_record", validate_record.func),
            name="validate_record",
            description=validate_record.description,
        ),
        StructuredTool.from_function(
            func=guard(session, "delete_record", delete_record.func),
            name="delete_record",
            description=delete_record.description,
        ),
    ]


if __name__ == "__main__":  # pragma: no cover
    # Two separate sessions on purpose: once a temporal contract is
    # violated it latches VIOLATED for the rest of that session (a
    # deliberate ACEL design choice — see README's "Shadow mode" section
    # for why), so the "should succeed" demo needs its own fresh session
    # rather than reusing the one that already tripped the contract below.
    blocked_session = build_session()
    blocked_delete = build_tools(blocked_session)[1]

    print("Calling delete_record BEFORE validate_record (should be blocked)...")
    try:
        blocked_delete.func(record_id="r_42")
        print("  ...ran without error (unexpected!)")
    except ContractViolation as exc:
        print(f"  BLOCKED: {exc.violation.message}")

    ok_session = build_session()
    validate, delete = build_tools(ok_session)

    print("\nCalling validate_record, then delete_record (should succeed)...")
    print(" ", validate.func(record_id="r_42"))
    print(" ", delete.func(record_id="r_42"))
