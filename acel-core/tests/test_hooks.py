"""Tests for acel.hooks — the Claude Code PreToolUse/PostToolUse integration.

Each hook fires as a fresh subprocess in real usage; these tests call the
core pretooluse()/posttooluse() functions directly (no subprocess), and
simulate "multiple invocations across one session" by just calling them
repeatedly against the same state_dir/session_id, exactly as separate
subprocess invocations would.
"""

from __future__ import annotations

import io
import json

import pytest

from acel.hooks import (
    DEFAULT_STATE_DIR,
    _safe_session_id,
    _trace_path,
    main_posttooluse,
    main_pretooluse,
    posttooluse,
    pretooluse,
)

RULES = {
    "contracts": [
        {"template": "must_precede", "args": ["Read", "Edit"]},
        {"template": "at_most_n_times", "args": ["Bash"], "kwargs": {"n": 2}},
    ]
}


def _write_rules(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(RULES))
    return str(path)


# ---------------------------------------------------------------------------
# pretooluse() / posttooluse() core logic
# ---------------------------------------------------------------------------


def test_pretooluse_allows_a_call_with_no_prior_history(tmp_path):
    rules_path = _write_rules(tmp_path)
    hook_input = {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": "a.py"}}

    result = pretooluse(hook_input, rules_path=rules_path, state_dir=tmp_path / "state")
    assert result == {}  # allow: no decision to report


def test_pretooluse_blocks_when_ordering_contract_would_be_violated(tmp_path):
    """Edit before Read: must_precede(Read, Edit) is violated on the very
    first call, since there's no prior history."""
    rules_path = _write_rules(tmp_path)
    hook_input = {"session_id": "s1", "tool_name": "Edit", "tool_input": {"file_path": "a.py"}}

    result = pretooluse(hook_input, rules_path=rules_path, state_dir=tmp_path / "state")
    assert "hookSpecificOutput" in result
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "must_precede" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_pretooluse_allows_edit_after_a_recorded_read(tmp_path):
    rules_path = _write_rules(tmp_path)
    state_dir = tmp_path / "state"

    # Simulate: Read happened and was recorded via posttooluse in an earlier
    # (separate, in real usage) subprocess invocation.
    posttooluse(
        {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": "a.py"}, "tool_response": {"ok": True}},
        state_dir=state_dir,
    )

    result = pretooluse(
        {"session_id": "s1", "tool_name": "Edit", "tool_input": {"file_path": "a.py"}},
        rules_path=rules_path,
        state_dir=state_dir,
    )
    assert result == {}


def test_pretooluse_blocks_after_cap_reached_via_recorded_history(tmp_path):
    """at_most_n_times(Bash, n=2): two prior Bash calls recorded, the third
    pretooluse check should be blocked."""
    rules_path = _write_rules(tmp_path)
    state_dir = tmp_path / "state"

    for _ in range(2):
        posttooluse(
            {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "echo hi"}, "tool_response": {"ok": True}},
            state_dir=state_dir,
        )

    result = pretooluse(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "echo again"}},
        rules_path=rules_path,
        state_dir=state_dir,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "at_most_n_times" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_pretooluse_sessions_are_isolated_by_session_id(tmp_path):
    """A cap tripped in one Claude Code session must not bleed into another."""
    rules_path = _write_rules(tmp_path)
    state_dir = tmp_path / "state"

    for _ in range(2):
        posttooluse(
            {"session_id": "session-A", "tool_name": "Bash", "tool_input": {}, "tool_response": None},
            state_dir=state_dir,
        )

    # session-A is now at its Bash cap...
    blocked = pretooluse(
        {"session_id": "session-A", "tool_name": "Bash", "tool_input": {}},
        rules_path=rules_path,
        state_dir=state_dir,
    )
    assert blocked["hookSpecificOutput"]["permissionDecision"] == "deny"

    # ...but session-B has no history at all, so it's unaffected.
    allowed = pretooluse(
        {"session_id": "session-B", "tool_name": "Bash", "tool_input": {}},
        rules_path=rules_path,
        state_dir=state_dir,
    )
    assert allowed == {}


def test_posttooluse_persists_trace_across_separate_invocations(tmp_path):
    """Each posttooluse() call here simulates a fresh subprocess — nothing
    is shared except the state_dir on disk."""
    state_dir = tmp_path / "state"
    posttooluse({"session_id": "s1", "tool_name": "Read", "tool_input": {"a": 1}, "tool_response": "r1"}, state_dir=state_dir)
    posttooluse({"session_id": "s1", "tool_name": "Write", "tool_input": {"a": 2}, "tool_response": "r2"}, state_dir=state_dir)

    trace = json.loads(_trace_path(state_dir, "s1").read_text())
    assert [t["tool"] for t in trace] == ["Read", "Write"]
    assert trace[0]["result"] == "r1"


def test_pretooluse_missing_fields_default_gracefully(tmp_path):
    rules_path = _write_rules(tmp_path)
    result = pretooluse({}, rules_path=rules_path, state_dir=tmp_path / "state")
    assert result == {}  # unknown tool, no history -> nothing to block


# ---------------------------------------------------------------------------
# Filesystem safety
# ---------------------------------------------------------------------------


def test_safe_session_id_strips_path_traversal_characters():
    assert _safe_session_id("../../etc/passwd") == "etcpasswd"
    assert _safe_session_id("normal-id_123") == "normal-id_123"


def test_safe_session_id_falls_back_when_everything_is_stripped():
    assert _safe_session_id("../../") == "unknown"


def test_trace_path_stays_inside_state_dir(tmp_path):
    state_dir = tmp_path / "state"
    path = _trace_path(state_dir, "../../evil")
    assert path.parent == state_dir


# ---------------------------------------------------------------------------
# CLI entry points (stdin/stdout handling)
# ---------------------------------------------------------------------------


def test_main_pretooluse_reads_stdin_and_prints_deny_json(tmp_path, monkeypatch, capsys):
    rules_path = _write_rules(tmp_path)
    hook_input = {"session_id": "s1", "tool_name": "Edit", "tool_input": {}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(hook_input)))

    exit_code = main_pretooluse(rules_path, state_dir=str(tmp_path / "state"))
    out = capsys.readouterr().out

    assert exit_code == 0
    printed = json.loads(out)
    assert printed["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_pretooluse_prints_nothing_when_allowed(tmp_path, monkeypatch, capsys):
    rules_path = _write_rules(tmp_path)
    hook_input = {"session_id": "s1", "tool_name": "Read", "tool_input": {}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(hook_input)))

    exit_code = main_pretooluse(rules_path, state_dir=str(tmp_path / "state"))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert out.strip() == ""


def test_main_posttooluse_records_from_stdin(tmp_path, monkeypatch):
    hook_input = {"session_id": "s1", "tool_name": "Read", "tool_input": {"a": 1}, "tool_response": "ok"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(hook_input)))

    state_dir = tmp_path / "state"
    exit_code = main_posttooluse(state_dir=str(state_dir))

    assert exit_code == 0
    trace = json.loads(_trace_path(state_dir, "s1").read_text())
    assert trace[0]["tool"] == "Read"


def test_main_pretooluse_handles_empty_stdin(tmp_path, monkeypatch):
    rules_path = _write_rules(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    exit_code = main_pretooluse(rules_path, state_dir=str(tmp_path / "state"))
    assert exit_code == 0


def test_default_state_dir_is_dot_claude_acel_state():
    assert DEFAULT_STATE_DIR == ".claude/acel_state"


def test_main_posttooluse_tolerates_a_leading_utf8_bom(tmp_path, monkeypatch):
    """Regression test: piping into a subprocess's stdin from PowerShell
    prepends a UTF-8 BOM that plain json.loads chokes on."""
    hook_input = {"session_id": "s1", "tool_name": "Read", "tool_input": {}, "tool_response": None}
    bom_prefixed = chr(0xFEFF) + json.dumps(hook_input)
    monkeypatch.setattr("sys.stdin", io.StringIO(bom_prefixed))

    state_dir = tmp_path / "state"
    exit_code = main_posttooluse(state_dir=str(state_dir))

    assert exit_code == 0
    trace = json.loads(_trace_path(state_dir, "s1").read_text())
    assert trace[0]["tool"] == "Read"


# ---------------------------------------------------------------------------
# CLI parser wiring
# ---------------------------------------------------------------------------


def test_hook_pretooluse_subcommand_registered():
    from acel.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["hook-pretooluse", "--rules", "rules.yaml"])
    assert args.command == "hook-pretooluse"
    assert args.rules == "rules.yaml"
    assert args.state_dir == DEFAULT_STATE_DIR


def test_hook_posttooluse_subcommand_registered():
    from acel.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["hook-posttooluse"])
    assert args.command == "hook-posttooluse"
    assert args.state_dir == DEFAULT_STATE_DIR


def test_pretooluse_blocks_commit_without_prior_test_run_content_matcher(tmp_path):
    """The flagship 'commit before test' scenario from the README, run
    through the actual hook pipeline (rules file -> pretooluse ->
    posttooluse), not just the temporal-contract unit test."""
    pytest.importorskip("yaml")
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        "contracts:\n"
        "  - template: must_precede\n"
        "    args:\n"
        "      - {tool: Bash, matches: \"pytest|npm (run )?test\"}\n"
        "      - {tool: Bash, matches: \"git commit\"}\n"
    )
    state_dir = tmp_path / "state"

    # Straight to a commit, no test run recorded -> blocked.
    result = pretooluse(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "git commit -m 'wip'"}},
        rules_path=str(rules_path),
        state_dir=state_dir,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretooluse_allows_commit_after_recorded_test_run_content_matcher(tmp_path):
    pytest.importorskip("yaml")
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        "contracts:\n"
        "  - template: must_precede\n"
        "    args:\n"
        "      - {tool: Bash, matches: \"pytest|npm (run )?test\"}\n"
        "      - {tool: Bash, matches: \"git commit\"}\n"
    )
    state_dir = tmp_path / "state"

    posttooluse(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "pytest -q"}, "tool_response": {"ok": True}},
        state_dir=state_dir,
    )

    result = pretooluse(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "git commit -m 'wip'"}},
        rules_path=str(rules_path),
        state_dir=state_dir,
    )
    assert result == {}


def test_pretooluse_unrelated_bash_calls_dont_satisfy_the_test_requirement(tmp_path):
    pytest.importorskip("yaml")
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        "contracts:\n"
        "  - template: must_precede\n"
        "    args:\n"
        "      - {tool: Bash, matches: \"pytest|npm (run )?test\"}\n"
        "      - {tool: Bash, matches: \"git commit\"}\n"
    )
    state_dir = tmp_path / "state"

    posttooluse(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls -la"}, "tool_response": {"ok": True}},
        state_dir=state_dir,
    )

    result = pretooluse(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "git commit -m 'wip'"}},
        rules_path=str(rules_path),
        state_dir=state_dir,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_pretooluse_end_to_end_through_cli(tmp_path, monkeypatch, capsys):
    from acel.cli import build_parser

    rules_path = _write_rules(tmp_path)
    hook_input = {"session_id": "s1", "tool_name": "Edit", "tool_input": {}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(hook_input)))

    parser = build_parser()
    args = parser.parse_args(
        ["hook-pretooluse", "--rules", rules_path, "--state-dir", str(tmp_path / "state")]
    )
    exit_code = args.func(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
