"""Tests for acel.config — loading contracts/state from JSON and YAML rules files."""

from __future__ import annotations

import pytest

from acel import config as config_mod

YAML_RULES = """\
state:
  authenticated: false

contracts:
  - template: must_precede
    args: [validate_record, delete_record]
  - template: at_most_n_times
    args: [send_payment]
    kwargs: {n: 1}
"""

JSON_RULES = """\
{
  "state": {"authenticated": false},
  "contracts": [
    {"template": "must_precede", "args": ["validate_record", "delete_record"]},
    {"template": "at_most_n_times", "args": ["send_payment"], "kwargs": {"n": 1}}
  ]
}
"""

LEGACY_JSON_RULES = """\
{
  "initial_state": {"authenticated": false},
  "contracts": [
    {"template": "required_before_session_end", "args": ["authenticate"]}
  ]
}
"""


def test_load_json_rules(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(JSON_RULES)

    rules = config_mod.load_rules(path)
    contracts = config_mod.contracts_from_rules(rules)
    state = config_mod.state_from_rules(rules)

    assert state == {"authenticated": False}
    specs = [c.spec for c in contracts]
    assert any("must_precede" in s for s in specs)
    assert any("at_most_n_times" in s for s in specs)


def test_load_yaml_rules(tmp_path):
    yaml = pytest.importorskip("yaml")
    del yaml  # only need to know it's installed
    path = tmp_path / "rules.yaml"
    path.write_text(YAML_RULES)

    rules = config_mod.load_rules(path)
    contracts = config_mod.contracts_from_rules(rules)
    state = config_mod.state_from_rules(rules)

    assert state == {"authenticated": False}
    assert len(contracts) == 2


def test_json_and_yaml_produce_equivalent_contracts(tmp_path):
    pytest.importorskip("yaml")
    json_path = tmp_path / "rules.json"
    yaml_path = tmp_path / "rules.yaml"
    json_path.write_text(JSON_RULES)
    yaml_path.write_text(YAML_RULES)

    json_specs = [c.spec for c in config_mod.load_contracts(json_path)]
    yaml_specs = [c.spec for c in config_mod.load_contracts(yaml_path)]
    assert json_specs == yaml_specs


def test_legacy_initial_state_key_still_works(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(LEGACY_JSON_RULES)

    rules = config_mod.load_rules(path)
    state = config_mod.state_from_rules(rules)
    contracts = config_mod.contracts_from_rules(rules)

    assert state == {"authenticated": False}
    assert len(contracts) == 1


def test_state_key_takes_precedence_over_initial_state(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"state": {"a": 1}, "initial_state": {"a": 2}}')

    rules = config_mod.load_rules(path)
    assert config_mod.state_from_rules(rules) == {"a": 1}


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(config_mod.ConfigError, match="no such rules file"):
        config_mod.load_rules(tmp_path / "nope.yaml")


def test_unrecognized_extension_raises_config_error(tmp_path):
    path = tmp_path / "rules.txt"
    path.write_text("{}")
    with pytest.raises(config_mod.ConfigError, match="unrecognized rules file extension"):
        config_mod.load_rules(path)


def test_invalid_json_raises_config_error(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text("{not valid json")
    with pytest.raises(config_mod.ConfigError, match="invalid JSON"):
        config_mod.load_rules(path)


def test_unknown_template_raises_config_error(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"contracts": [{"template": "not_a_real_template", "args": []}]}')
    with pytest.raises(config_mod.ConfigError, match="contracts\\[0\\]"):
        config_mod.load_contracts(path)


def test_contracts_not_a_list_raises_config_error(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"contracts": "nope"}')
    with pytest.raises(config_mod.ConfigError, match="'contracts' must be a list"):
        config_mod.load_contracts(path)


def test_empty_rules_file_is_valid(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text("{}")

    rules = config_mod.load_rules(path)
    assert config_mod.contracts_from_rules(rules) == []
    assert config_mod.state_from_rules(rules) == {}


def test_top_level_not_a_mapping_raises_config_error(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(config_mod.ConfigError, match="top level must be a mapping"):
        config_mod.load_rules(path)


# --- groups --------------------------------------------------------------

GROUPED_RULES = """\
{
  "groups": {
    "refund_policy": [
      {"template": "must_precede", "args": ["verify_customer", "issue_refund"]},
      {"template": "at_most_total", "args": ["issue_refund", "amount"], "kwargs": {"limit": 500}}
    ]
  },
  "contracts": [
    {"template": "must_precede", "args": ["open_ticket", "close_ticket"]},
    {"group": "refund_policy"}
  ]
}
"""


def test_group_reference_expands_into_flat_contracts(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(GROUPED_RULES)

    rules = config_mod.load_rules(path)
    contracts = config_mod.contracts_from_rules(rules)

    specs = [c.spec for c in contracts]
    assert len(specs) == 3
    assert any("open_ticket" in s for s in specs)
    assert any("verify_customer" in s for s in specs)
    assert any("at_most_total" in s for s in specs)


def test_contracts_by_group_from_rules_returns_named_bundles(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(GROUPED_RULES)

    rules = config_mod.load_rules(path)
    groups = config_mod.contracts_by_group_from_rules(rules)

    assert set(groups) == {"refund_policy"}
    assert len(groups["refund_policy"]) == 2
    specs = [c.spec for c in groups["refund_policy"]]
    assert any("verify_customer" in s for s in specs)
    assert any("at_most_total" in s for s in specs)


def test_declared_but_unreferenced_group_has_no_effect(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(
        '{"groups": {"unused": [{"template": "must_precede", "args": ["a", "b"]}]}, '
        '"contracts": []}'
    )
    rules = config_mod.load_rules(path)
    assert config_mod.contracts_from_rules(rules) == []
    assert list(config_mod.contracts_by_group_from_rules(rules)) == ["unused"]


def test_unknown_group_reference_raises_config_error(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"contracts": [{"group": "does_not_exist"}]}')
    with pytest.raises(config_mod.ConfigError, match="unknown group"):
        config_mod.contracts_from_rules(config_mod.load_rules(path))


def test_two_references_to_same_group_get_independent_instances(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(
        '{"groups": {"g": [{"template": "at_most_n_times", "args": ["pay"], "kwargs": {"n": 1}}]}, '
        '"contracts": [{"group": "g"}, {"group": "g"}]}'
    )
    rules = config_mod.load_rules(path)
    contracts = config_mod.contracts_from_rules(rules)
    assert len(contracts) == 2
    assert contracts[0] is not contracts[1]  # independent automaton state, not shared


def test_groups_not_a_mapping_raises_config_error(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"groups": ["nope"]}')
    with pytest.raises(config_mod.ConfigError, match="'groups' must be a mapping"):
        config_mod.contracts_from_rules(config_mod.load_rules(path))


def test_group_specs_not_a_list_raises_config_error(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"groups": {"g": "nope"}}')
    with pytest.raises(config_mod.ConfigError, match=r"groups\['g'\] must be a list"):
        config_mod.contracts_from_rules(config_mod.load_rules(path))


# ---------------------------------------------------------------------------
# Declarative content-aware matchers ({"tool": ..., "matches": ...} in args)
# ---------------------------------------------------------------------------

CONTENT_MATCHER_YAML = """\
contracts:
  - template: must_precede
    args:
      - {tool: Bash, matches: "pytest|npm (run )?test"}
      - {tool: Bash, matches: "git commit"}
"""


def test_content_matcher_dict_in_args_builds_a_working_contract(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "rules.yaml"
    path.write_text(CONTENT_MATCHER_YAML)
    contracts = config_mod.contracts_from_rules(config_mod.load_rules(path))
    assert len(contracts) == 1

    contract = contracts[0]
    # A commit with no prior test run violates.
    from acel.verdict import Verdict

    assert contract.on_event("Bash", {"command": "git commit -m 'wip'"}) is Verdict.VIOLATED


def test_content_matcher_dict_allows_when_test_ran_first(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "rules.yaml"
    path.write_text(CONTENT_MATCHER_YAML)
    contracts = config_mod.contracts_from_rules(config_mod.load_rules(path))
    contract = contracts[0]

    from acel.verdict import Verdict

    assert contract.on_event("Bash", {"command": "pytest -q"}) is not Verdict.VIOLATED
    assert contract.on_event("Bash", {"command": "git commit -m 'wip'"}) is not Verdict.VIOLATED


def test_content_matcher_dict_defaults_field_to_command(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"contracts": [{"template": "at_most_n_times", "args": [{"tool": "Bash", "matches": "rm"}], "kwargs": {"n": 0}}]}')
    contracts = config_mod.contracts_from_rules(config_mod.load_rules(path))
    contract = contracts[0]

    from acel.verdict import Verdict

    assert contract.on_event("Bash", {"command": "rm file.txt"}) is Verdict.VIOLATED


def test_content_matcher_dict_custom_field(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(
        '{"contracts": [{"template": "at_most_n_times", '
        '"args": [{"tool": "Write", "matches": "\\\\.env$", "field": "file_path"}], '
        '"kwargs": {"n": 0}}]}'
    )
    contracts = config_mod.contracts_from_rules(config_mod.load_rules(path))
    contract = contracts[0]

    from acel.verdict import Verdict

    # A non-matching write never trips it (checked first — the contract
    # latches VIOLATED permanently once tripped, so order matters here).
    assert contract.on_event("Write", {"file_path": "config/other.py"}) is not Verdict.VIOLATED
    assert contract.on_event("Write", {"file_path": "config/.env"}) is Verdict.VIOLATED


def test_content_matcher_dict_missing_matches_key_raises_config_error(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"contracts": [{"template": "at_most_n_times", "args": [{"tool": "Bash"}], "kwargs": {"n": 1}}]}')
    with pytest.raises(config_mod.ConfigError, match="tool.*matches"):
        config_mod.contracts_from_rules(config_mod.load_rules(path))


def test_content_matcher_dict_missing_tool_key_raises_config_error(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"contracts": [{"template": "at_most_n_times", "args": [{"matches": "rm"}], "kwargs": {"n": 1}}]}')
    with pytest.raises(config_mod.ConfigError):
        config_mod.contracts_from_rules(config_mod.load_rules(path))


def test_plain_string_args_still_work_alongside_content_matchers(tmp_path):
    """Regression guard: adding matcher-dict support shouldn't break the
    existing plain-string args path."""
    path = tmp_path / "rules.json"
    path.write_text('{"contracts": [{"template": "must_precede", "args": ["validate", "delete"]}]}')
    contracts = config_mod.contracts_from_rules(config_mod.load_rules(path))
    assert len(contracts) == 1
    assert "must_precede(validate, delete)" in contracts[0].spec
