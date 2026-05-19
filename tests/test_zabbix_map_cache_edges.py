"""zabbix_map cache dict format."""

import json

from zabbix_map import load_zabbix_cache, save_zabbix_cache


def test_load_zabbix_cache_dict_format(tmp_path):
    p = tmp_path / "cache.json"
    data = {
        "host_id_by_name": {"h1": "101"},
        "items_by_host_iface": {"h1|eth1": {"bits_in": "k1"}},
    }
    p.write_text(json.dumps(data), encoding="utf-8")
    h, items = load_zabbix_cache(str(p))
    assert h["h1"] == "101"
    assert ("h1", "eth1") in items


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "c.json"
    hosts = {"ALA": "1"}
    items = {("ALA", "eth1"): {"bits_in": "x"}}
    save_zabbix_cache(str(p), hosts, items)
    h2, i2 = load_zabbix_cache(str(p))
    assert h2 == hosts
    assert i2[("ALA", "eth1")]["bits_in"] == "x"
