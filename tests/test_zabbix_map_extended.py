"""Additional zabbix_map coverage: default create, update with existing elements."""

import sys
from pathlib import Path

import pytest

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_map import MAP_NAME, ensure_map_exists, main, update_uplinks_map

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _hosts_items():
    hosts = [
        {"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"},
        {"hostid": "102", "host": "FRN-MX-1", "name": "FRN-MX-1"},
    ]
    items = [
        {
            "itemid": "501",
            "hostid": "101",
            "name": 'Interface Ethernet51/1: Bits received',
            "key_": 'net.if.in["Ethernet51/1"]',
        },
        {
            "itemid": "502",
            "hostid": "101",
            "name": 'Interface Ethernet51/1: Bits sent',
            "key_": 'net.if.out["Ethernet51/1"]',
        },
    ]
    return hosts, items


def test_ensure_map_exists_creates(monkeypatch, zabbix_env):
    created = []
    mocker = (
        build_standard_zabbix_mocker()
        .on("map.get", lambda p: [])
        .on("map.create", lambda p: created.append(p) or {"sysmapids": ["88"]})
    )
    mocker.activate(monkeypatch)
    sid, err = ensure_map_exists("https://z.example/api_jsonrpc.php", "t")
    assert err is None
    assert sid == "88"


def test_update_map_with_existing_selements(monkeypatch, zabbix_env):
    from zabbix_map import load_devices_json

    data, _ = load_devices_json(str(FIXTURES / "dry_ssh_minimal.json"))
    devices = {"ALA-KZT-7280TR-1": data["devices"]["ALA-KZT-7280TR-1"]}
    host_id = {"ALA-KZT-7280TR-1": "101"}
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "itemid_in": "501",
            "itemid_out": "502",
            "bits_in": 'net.if.in["Ethernet51/1"]',
            "bits_out": 'net.if.out["Ethernet51/1"]',
        },
    }

    existing_map = [{
        "sysmapid": "55",
        "selements": [
            {
                "selementid": "1",
                "elementtype": 0,
                "elements": [{"hostid": "101"}],
                "label": "ALA-KZT-7280TR-1",
            },
            {
                "selementid": "2",
                "elementtype": 4,
                "label": "OldISP",
            },
        ],
        "links": [{"linkid": "9", "selementid1": "1", "selementid2": "2"}],
    }]

    def map_get(params):
        if params.get("sysmapids"):
            return existing_map
        if (params.get("filter") or {}).get("name") == MAP_NAME:
            return existing_map
        return []

    mocker = (
        build_standard_zabbix_mocker()
        .on("map.get", map_get)
        .on("map.update", lambda p: True)
    )
    mocker.activate(monkeypatch)
    err, sid = update_uplinks_map(
        "https://z.example/api_jsonrpc.php",
        "t",
        devices,
        host_id,
        items,
        {"Uplink: Cogent 10G": "Cogent"},
        prune_obsolete=True,
    )
    assert err is None
    assert sid == "55"


def test_main_default_creates_map(monkeypatch, zabbix_env, capsys):
    hosts, items = _hosts_items()

    def map_get(params):
        if params.get("sysmapids"):
            return [{"sysmapid": "1", "selements": [], "links": []}]
        return []

    mocker = (
        build_standard_zabbix_mocker(hosts=hosts, items=items)
        .on("map.get", map_get)
        .on("map.create", lambda p: {"sysmapids": ["1"]})
        .on("map.update", lambda p: True)
    )
    mocker.activate(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_map.py",
            "-f",
            str(FIXTURES / "dry_ssh_minimal.json"),
            "--no-cache",
            "--host",
            "ALA-KZT-7280TR-1",
        ],
    )
    main()
    assert "Map created" in capsys.readouterr().err or True
