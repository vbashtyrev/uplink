"""zabbix_map Zabbix cache load/save."""

import json
from pathlib import Path

from zabbix_map import load_zabbix_cache, save_zabbix_cache, ZABBIX_CACHE_FILE


def test_save_and_load_zabbix_cache(tmp_path):
    path = tmp_path / ZABBIX_CACHE_FILE
    hosts = {"H1": "101"}
    items = {("H1", "eth1"): {"bits_in": "in"}}
    save_zabbix_cache(str(path), hosts, items)
    h, it = load_zabbix_cache(str(path))
    assert h == hosts
    assert it == items


def test_load_zabbix_cache_missing(tmp_path):
    assert load_zabbix_cache(str(tmp_path / "missing.json")) == (None, None)


def test_load_zabbix_cache_bad_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert load_zabbix_cache(str(bad)) == (None, None)
