"""Additional zabbix_map coverage: default create, update with existing elements."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.mocks.inventory_scope import (
    dry_ssh_minimal_inventory_context,
    write_dry_ssh_minimal_inventory,
)
from tests.mocks.map_state import MapStateTracker
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
    from uplinks.data import load_devices_json

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

    map_tracker = MapStateTracker(
        sysmapid="55",
        selements=existing_map[0]["selements"],
        links=existing_map[0]["links"],
    )

    mocker = (
        build_standard_zabbix_mocker()
        .on("map.get", map_tracker.map_get)
        .on("map.update", map_tracker.map_update)
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
        device_iface_to_provider=dry_ssh_minimal_inventory_context()["device_iface_to_provider"],
    )
    assert err is None
    assert sid == "55"


def test_main_default_creates_map(monkeypatch, zabbix_env, tmp_path, capsys):
    hosts, items = _hosts_items()

    map_tracker = MapStateTracker(sysmapid="1", exists=False)

    mocker = (
        build_standard_zabbix_mocker(hosts=hosts, items=items)
        .on("map.get", map_tracker.map_get)
        .on("map.create", map_tracker.map_create)
        .on("map.update", map_tracker.map_update)
    )
    mocker.activate(monkeypatch)
    inv = write_dry_ssh_minimal_inventory(tmp_path)
    with patch("zabbix_map.load_uplink_provider_context", return_value=dry_ssh_minimal_inventory_context()):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "zabbix_map.py",
                "--inventory-file",
                str(inv),
                "--no-cache",
                "--host",
                "ALA-KZT-7280TR-1",
            ],
        )
        main()
    err_out = capsys.readouterr().err
    assert map_tracker.creates or map_tracker.updates
    assert map_tracker.width is not None and map_tracker.width >= 1
    assert map_tracker.height is not None and map_tracker.height >= 1
    assert "created" in err_out.lower() or "sysmapid" in err_out.lower()
