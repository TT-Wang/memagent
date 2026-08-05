"""Tests for taskdag.config."""

import json

import pytest

from taskdag.config import (
    CONFIG,
    VALID_LOG_LEVELS,
    WARNINGS,
    dump,
    get,
    get_int,
    load,
    set_key,
    validate,
)


def test_worker_count_seeded_to_four():
    assert CONFIG["worker_count"] == 4
    assert get("worker_count") == 4


def test_get_returns_default_for_missing_key():
    assert get("nonexistent") is None
    assert get("nonexistent", "fallback") == "fallback"


def test_get_returns_stored_value_over_default():
    set_key("worker_count", 2)
    assert get("worker_count", 99) == 2
    set_key("worker_count", 4)  # restore seed for other tests


def test_set_key_adds_new_entry():
    set_key("timeout", 30)
    assert CONFIG["timeout"] == 30
    assert get("timeout") == 30
    del CONFIG["timeout"]  # keep CONFIG schema-clean for validate()


def test_set_key_overwrites_existing_entry():
    set_key("worker_count", 8)
    assert get("worker_count") == 8
    set_key("worker_count", 4)  # restore seed
    assert get("worker_count") == 4


def test_validate_passes_on_seeded_config():
    validate()


def test_validate_accepts_int_values():
    set_key("worker_count", 8)
    set_key("wave_pause_ms", 500)
    try:
        validate()
    finally:
        set_key("worker_count", 4)
        set_key("wave_pause_ms", 50)


def test_validate_rejects_non_int_count_key():
    set_key("worker_count", "4")
    try:
        with pytest.raises(ValueError, match="worker_count"):
            validate()
    finally:
        set_key("worker_count", 4)


def test_validate_rejects_non_int_ms_key():
    set_key("wave_pause_ms", 50.5)
    try:
        with pytest.raises(ValueError, match="wave_pause_ms"):
            validate()
    finally:
        set_key("wave_pause_ms", 50)


def test_validate_rejects_bool_for_count_key():
    set_key("worker_count", True)
    try:
        with pytest.raises(ValueError, match="worker_count"):
            validate()
    finally:
        set_key("worker_count", 4)


def test_validate_rejects_unknown_log_level():
    set_key("log_level", "verbose")
    try:
        with pytest.raises(ValueError, match="log level"):
            validate()
    finally:
        set_key("log_level", "info")


def test_validate_accepts_all_known_log_levels():
    original = CONFIG["log_level"]
    try:
        for level in VALID_LOG_LEVELS:
            set_key("log_level", level)
            validate()
    finally:
        set_key("log_level", original)


def test_dump_writes_config_to_json(tmp_path):
    path = tmp_path / "config.json"
    dump(path)
    assert json.loads(path.read_text()) == CONFIG


def test_dump_load_round_trip_restores_config(tmp_path):
    path = tmp_path / "config.json"
    dump(path)
    original = dict(CONFIG)
    CONFIG.clear()
    load(path)
    assert CONFIG == original


def test_load_replaces_config(tmp_path):
    original = dict(CONFIG)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "worker_count": 2,
                "wave_pause_ms": 10,
                "log_level": "debug",
                "qz_max_queue_depth": 64,
                "qz_batch_flush_size": 16,
            }
        )
    )
    try:
        load(path)
        assert CONFIG == {
            "worker_count": 2,
            "wave_pause_ms": 10,
            "log_level": "debug",
            "qz_max_queue_depth": 64,
            "qz_batch_flush_size": 16,
        }
    finally:
        CONFIG.clear()
        CONFIG.update(original)


def test_load_rejects_invalid_values_and_leaves_config_unchanged(tmp_path):
    original = dict(CONFIG)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "worker_count": "four",
                "wave_pause_ms": 50,
                "log_level": "info",
                "qz_max_queue_depth": 128,
                "qz_batch_flush_size": 32,
            }
        )
    )
    with pytest.raises(ValueError, match="worker_count"):
        load(path)
    assert CONFIG == original


def test_load_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "nope.json")


def test_get_int_returns_int_for_int_value():
    assert get_int("worker_count") == 4
    assert isinstance(get_int("worker_count"), int)


def test_get_int_coerces_numeric_string():
    set_key("qz_max_queue_depth", "64")
    try:
        assert get_int("qz_max_queue_depth") == 64
        assert isinstance(get_int("qz_max_queue_depth"), int)
    finally:
        set_key("qz_max_queue_depth", 128)


def test_get_int_raises_key_error_on_missing_key():
    with pytest.raises(KeyError):
        get_int("no_such_key")


def test_get_int_rejects_non_int_value():
    set_key("qz_batch_flush_size", "many")
    try:
        with pytest.raises(ValueError):
            get_int("qz_batch_flush_size")
    finally:
        set_key("qz_batch_flush_size", 32)


def test_get_int_rejects_bool_value():
    # qz_color_output is seeded True; a bool is not an int here.
    with pytest.raises(ValueError):
        get_int("qz_color_output")


def test_validate_no_warnings_for_known_keys():
    # every seeded key -- including the qz_ ones -- is known
    validate()
    assert WARNINGS == []


def test_validate_warns_on_unknown_qz_prefixed_key():
    set_key("qz_frobnicate", 1)
    try:
        validate()  # tolerated: must not raise
        assert any("qz_frobnicate" in w for w in WARNINGS)
    finally:
        del CONFIG["qz_frobnicate"]


def test_validate_warning_message_names_the_key():
    set_key("qz_mystery", True)
    try:
        validate()
        assert WARNINGS == ["unknown qz_ config key 'qz_mystery'"]
    finally:
        del CONFIG["qz_mystery"]


def test_validate_warns_for_each_unknown_qz_key_in_order():
    set_key("qz_first", 1)
    set_key("qz_second", 2)
    try:
        validate()
        assert WARNINGS == [
            "unknown qz_ config key 'qz_first'",
            "unknown qz_ config key 'qz_second'",
        ]
    finally:
        del CONFIG["qz_first"]
        del CONFIG["qz_second"]


def test_validate_rejects_unknown_non_qz_key():
    set_key("mystery", 1)
    try:
        with pytest.raises(ValueError, match="unknown config key 'mystery'"):
            validate()
    finally:
        del CONFIG["mystery"]


def test_validate_clears_warnings_before_each_call():
    set_key("qz_first", 1)
    try:
        validate()
        assert WARNINGS == ["unknown qz_ config key 'qz_first'"]
    finally:
        del CONFIG["qz_first"]
    validate()
    assert WARNINGS == []


def test_validate_unknown_qz_key_does_not_relax_known_key_checks():
    set_key("qz_frobnicate", 1)
    set_key("wave_pause_ms", "50")
    try:
        with pytest.raises(ValueError, match="wave_pause_ms"):
            validate()
    finally:
        del CONFIG["qz_frobnicate"]
        set_key("wave_pause_ms", 50)


def test_qz_demo_retry_limit_seeded_to_three():
    assert CONFIG["qz_demo_retry_limit"] == 3
    assert get("qz_demo_retry_limit") == 3


def test_validate_unknown_qz_key_skips_type_check_for_its_value():
    # an unknown qz_ key is tolerated whatever its value's type
    set_key("qz_anything", object())
    try:
        validate()
        assert any("qz_anything" in w for w in WARNINGS)
    finally:
        del CONFIG["qz_anything"]


def test_load_tolerates_unknown_qz_key_with_warning(tmp_path):
    original = dict(CONFIG)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "worker_count": 2,
                "wave_pause_ms": 10,
                "log_level": "info",
                "qz_unknown_new": 1,
            }
        )
    )
    try:
        load(path)
        assert CONFIG["qz_unknown_new"] == 1
        assert WARNINGS == ["unknown qz_ config key 'qz_unknown_new'"]
    finally:
        CONFIG.clear()
        CONFIG.update(original)


def test_load_rejects_unknown_non_qz_key(tmp_path):
    original = dict(CONFIG)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mystery": 1}))
    with pytest.raises(ValueError, match="unknown config key 'mystery'"):
        load(path)
    assert CONFIG == original  # load validated before mutating
