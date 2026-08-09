"""ACEL guarding OpenAI-style function/tool calls — no MCP, no LangChain.

Proves the same "works with any Python agent loop" claim as
``langchain_agent_example.py``, this time for the raw OpenAI-compatible
chat completions ``tool_calls`` shape directly — the format an LLM response
actually comes back in before any framework wraps it, so this is the
lowest-level integration point: if you're hand-rolling your own agent loop
around a chat completions API, this is what wiring ACEL into it looks like.

No API key or network access needed to run this — it uses hand-built
``tool_calls`` dicts shaped exactly like what the API would return, so the
gate behavior is demonstrated without an actual model in the loop:

    python examples/openai_function_calling_example.py

A real agent loop would replace the hard-coded ``tool_calls`` list below
with ``response.choices[0].message.tool_calls`` from an actual API call —
everything from ``guard_openai_tool_call`` onward is unchanged either way.
"""

from __future__ import annotations

from acel import Session, at_most_total, must_precede
from acel.adapters import guard_openai_tool_call
from acel.violations import ContractViolation


def build_session() -> Session:
    session = Session()
    session.add_contract(must_precede("verify_customer", "issue_refund"))
    session.add_contract(at_most_total("issue_refund", "amount", limit=500))
    return session


def verify_customer(customer_id: str) -> dict:
    return {"customer_id": customer_id, "verified": True}


def issue_refund(customer_id: str, amount: float) -> dict:
    return {"customer_id": customer_id, "refunded": amount}


TOOLS = {"verify_customer": verify_customer, "issue_refund": issue_refund}


def handle_tool_call(session: Session, tool_call: dict) -> dict:
    """What a real agent loop's tool-dispatch step looks like with ACEL
    wired in: look up the real function by name, gate the call through
    ``session`` via :func:`guard_openai_tool_call`, return its result (or
    let a violation propagate/be reported, same as the plain-Python and
    LangChain examples)."""
    name = tool_call["function"]["name"]
    func = TOOLS[name]
    return guard_openai_tool_call(session, tool_call, func)


if __name__ == "__main__":  # pragma: no cover
    # Shaped exactly like `response.choices[0].message.tool_calls` from an
    # OpenAI-compatible chat completions API — a coding-agent scenario
    # where the model decided to issue a refund without ever verifying the
    # customer first (arguments are a JSON string, matching the real API).
    unverified_call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "issue_refund",
            "arguments": '{"customer_id": "c_42", "amount": 100}',
        },
    }

    print("Model tried to call issue_refund before verify_customer (should be blocked)...")
    session = build_session()
    try:
        handle_tool_call(session, unverified_call)
        print("  ...ran without error (unexpected!)")
    except ContractViolation as exc:
        print(f"  BLOCKED: {exc.violation.message}")

    print("\nCorrect order: verify_customer, then issue_refund (should succeed)...")
    session = build_session()
    verify_call = {
        "id": "call_2",
        "type": "function",
        "function": {"name": "verify_customer", "arguments": '{"customer_id": "c_42"}'},
    }
    refund_call = {
        "id": "call_3",
        "type": "function",
        "function": {"name": "issue_refund", "arguments": {"customer_id": "c_42", "amount": 100}},
    }
    print(" ", handle_tool_call(session, verify_call))
    print(" ", handle_tool_call(session, refund_call))

    print("\nA second refund that would push the session total over the $500 cap (should be blocked)...")
    big_refund_call = {
        "id": "call_4",
        "type": "function",
        "function": {"name": "issue_refund", "arguments": {"customer_id": "c_42", "amount": 450}},
    }
    try:
        handle_tool_call(session, big_refund_call)
        print("  ...ran without error (unexpected!)")
    except ContractViolation as exc:
        print(f"  BLOCKED: {exc.violation.message}")
