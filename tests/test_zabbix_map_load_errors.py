"""zabbix_map load_devices_json error paths."""

import json
from pathlib import Path

from zabbix_map import load_devices_json

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_load_devices_missing(tmp_path):
    data, err = load_devices_json(str(tmp_path / "nope.json"))
    assert data is None
    assert "not found" in err


def test_load_devices_bad_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    data, err = load_devices_json(str(bad))
    assert data is None
    assert "JSON" in err


def test_load_devices_no_devices_key(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"other": 1}', encoding="utf-8")
    data, err = load_devices_json(str(p))
    assert data is None
    assert "devices" in err
