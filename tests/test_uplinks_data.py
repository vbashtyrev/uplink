"""Tests for uplinks.data loaders."""

import json

from uplinks.data import (
    DEFAULT_DRY_SSH_FILE,
    load_description_map,
    load_devices_json,
)


def test_load_devices_json_ok(tmp_path):
    p = tmp_path / "dry.json"
    p.write_text(json.dumps({"devices": {"h1": []}}), encoding="utf-8")
    data, err = load_devices_json(str(p))
    assert err is None
    assert "devices" in data


def test_load_devices_json_missing_devices_key(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{}", encoding="utf-8")
    data, err = load_devices_json(str(p))
    assert data is None
    assert "devices" in err


def test_load_description_map_missing_returns_empty(tmp_path):
    assert load_description_map(str(tmp_path / "nope.json")) == {}


def test_default_filenames():
    assert DEFAULT_DRY_SSH_FILE == "dry-ssh.json"
