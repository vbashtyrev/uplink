"""zabbix_map main: default create map when missing."""

import sys
from pathlib import Path

import pytest

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_map import MAP_NAME, main as map_main

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_default_creates_map_when_absent(monkeypatch, zabbix_env, capsys):
    created = []

    def map_get(params):
        if params.get("filter", {}).get("name") == MAP_NAME:
            return []
        return [{"sysmapid": "55", "selements": [], "links": []}]

    mocker = build_standard_zabbix_mocker()
    mocker.on("map.get", map_get).on("map.create", lambda p: created.append(p) or {"sysmapids": ["55"]})
    mocker.on("map.update", lambda p: True).activate(monkeypatch)
    monkeypatch.setattr("zabbix_map.get_provider_aggregate_triggers", lambda *a, **k: {})
    monkeypatch.setattr("zabbix_map.get_link_commit_triggers", lambda *a, **k: {})
    host_id = {"ALA-KZT-7280TR-1": "101", "FRN-MX-1": "102"}
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {"bits_in": "in", "bits_out": "out"},
        ("FRN-MX-1", "ae5.0"): {"bits_in": "in", "bits_out": "out"},
    }
    monkeypatch.setattr(
        "zabbix_map.fetch_zabbix_hosts_and_items",
        lambda *a, **k: (host_id, items, None),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["zabbix_map.py", "-f", str(FIXTURES / "dry_ssh_minimal.json"), "--no-cache"],
    )
    map_main()
    err = capsys.readouterr().err
    assert "created" in err.lower() or "sysmapid" in err
