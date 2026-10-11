"""Configuration, rule matching and unit-conversion tests."""
from __future__ import annotations

import json

import pytest

from limitador.rules import AppRule, Config, Settings, config_path
from limitador.units import format_bytes, format_rate, parse_rate, to_display

MB = 1024 * 1024


def test_round_trip_save_and_load(tmp_path):
    path = tmp_path / "config.json"
    config = Config(
        settings=Settings(global_down_bps=2 * MB, global_up_bps=0.5 * MB,
                          max_delay_ms=30, exempt_control=False),
        rules=[AppRule(exe=r"C:\Program Files\App\app.exe", name="App",
                       down_bps=1 * MB, up_bps=100 * 1024)],
    )
    config.save(path)
    loaded = Config.load(path)
    assert loaded.settings.global_down_bps == 2 * MB
    assert loaded.settings.exempt_control is False
    assert loaded.settings.max_delay_ms == 30
    assert len(loaded.rules) == 1
    assert loaded.rules[0].name == "App"
    assert loaded.rules[0].down_bps == 1 * MB


def test_missing_or_broken_file_gives_defaults(tmp_path):
    assert Config.load(tmp_path / "nope.json").rules == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert Config.load(broken).settings.global_down_bps == 0


def test_rule_matching_ignores_path_case(monkeypatch):
    config = Config()
    config.set_rule(AppRule(exe=r"C:\Program Files\App\APP.exe", down_bps=MB))
    assert config.rule_for_exe(r"c:\program files\app\app.exe") is not None
    assert config.rule_for_exe(r"C:\otra\cosa.exe") is None
    assert config.rule_for_exe(None) is None


def test_set_rule_replaces_and_remove_works():
    config = Config()
    config.set_rule(AppRule(exe=r"C:\a\app.exe", down_bps=MB))
    config.set_rule(AppRule(exe=r"C:\a\app.exe", down_bps=2 * MB))
    assert len(config.rules) == 1 and config.rules[0].down_bps == 2 * MB
    assert config.remove_rule(r"C:\a\app.exe") is True
    assert config.remove_rule(r"C:\a\app.exe") is False
    assert config.rules == []


def test_only_rules_with_a_limit_are_active():
    config = Config()
    config.set_rule(AppRule(exe=r"C:\a\a.exe", down_bps=0, up_bps=0))
    config.set_rule(AppRule(exe=r"C:\a\b.exe", down_bps=MB))
    config.set_rule(AppRule(exe=r"C:\a\c.exe", down_bps=MB, enabled=False))
    active = config.active_rules()
    assert len(active) == 1
    assert active[0].exe.endswith("b.exe")


def test_settings_are_clamped():
    settings = Settings(global_down_bps=-5, max_delay_ms=9999)
    settings.clamp()
    assert settings.global_down_bps == 0
    assert settings.max_delay_ms == 500


def test_saved_json_is_human_readable(tmp_path):
    path = tmp_path / "config.json"
    Config(settings=Settings(global_down_bps=MB)).save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["settings"]["global_down_bps"] == MB
    assert "version" in data


def test_config_path_is_under_appdata():
    assert config_path().name == "config.json"
    assert "Limitador" in str(config_path())


@pytest.mark.parametrize("text,unit,expected", [
    ("1", "MB/s", MB),
    ("0", "MB/s", 0),
    ("", "MB/s", 0),
    ("1,5", "MB/s", 1.5 * MB),
    ("1.5", "MB/s", 1.5 * MB),
    ("512", "KB/s", 512 * 1024),
    ("8", "Mbit/s", 1_000_000),
])
def test_parse_rate(text, unit, expected):
    assert parse_rate(text, unit) == pytest.approx(expected)


def test_parse_rate_rejects_garbage_and_negatives():
    with pytest.raises(ValueError):
        parse_rate("mucho")
    with pytest.raises(ValueError):
        parse_rate("-3")


def test_formatting_helpers():
    assert format_rate(0) == "sin límite"
    assert format_rate(2 * MB) == "2 MB/s"
    assert format_rate(512 * 1024) == "512 KB/s"
    assert format_bytes(0) == "0 B"
    assert format_bytes(2 * 1024 ** 3) == "2.00 GB"
    assert to_display(0) == ""
    assert to_display(1.5 * MB) == "1.5"
