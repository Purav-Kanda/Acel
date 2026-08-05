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
