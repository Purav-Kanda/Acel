"""Tests for acel.matchers — content-aware tool matchers."""

from __future__ import annotations

from acel.matchers import ContentMatch, matching


def test_content_match_matches_tool_and_predicate():
    m = ContentMatch("Bash", lambda args: args.get("command") == "rm -rf /")
    assert m("Bash", {"command": "rm -rf /"}) is True


def test_content_match_false_when_tool_name_differs():
    m = ContentMatch("Bash", lambda args: True)
    assert m("Write", {}) is False


def test_content_match_false_when_predicate_false():
    m = ContentMatch("Bash", lambda args: False)
    assert m("Bash", {}) is False


def test_content_match_fails_closed_on_throwing_predicate():
    def bad(args):
        raise RuntimeError("boom")

    m = ContentMatch("Bash", bad)
    assert m("Bash", {}) is False  # errored predicate treated as no-match, not a crash


def test_content_match_default_label():
    m = ContentMatch("Bash", lambda args: True)
    assert "Bash" in str(m)


def test_content_match_custom_label():
    m = ContentMatch("Bash", lambda args: True, label="my custom label")
    assert str(m) == "my custom label"


def test_content_match_repr_is_debug_friendly():
    m = ContentMatch("Bash", lambda args: True, label="x")
    assert "ContentMatch" in repr(m)


def test_matching_builds_a_working_regex_matcher():
    m = matching("Bash", r"pytest")
    assert m("Bash", {"command": "pytest -q tests/"}) is True
    assert m("Bash", {"command": "echo hi"}) is False


def test_matching_uses_command_field_by_default():
    m = matching("Bash", r"foo")
    assert m("Bash", {"command": "foo"}) is True
    assert m("Bash", {"other_field": "foo"}) is False


def test_matching_accepts_custom_field():
    m = matching("Edit", r"secret", field="file_path")
    assert m("Edit", {"file_path": "src/secrets.py"}) is True
    assert m("Edit", {"file_path": "src/main.py"}) is False


def test_matching_stringifies_non_string_field_values():
    m = matching("Custom", r"42", field="count")
    assert m("Custom", {"count": 42}) is True


def test_matching_missing_field_is_no_match_not_error():
    m = matching("Bash", r"pytest")
    assert m("Bash", {}) is False


def test_matching_label_includes_tool_field_and_pattern():
    m = matching("Bash", r"pytest", field="command")
    assert str(m) == "Bash(command~/pytest/)"
