"""Integration tests for zabbix_map main() and update_uplinks_map."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from zabbix_map import (
    MAP_NAME,
    load_devices_json,
    main,
    update_uplinks_map,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


def _dry_ssh_devices():
    data, err = load_devices_json(str(FIXTURES / "dry_ssh_minimal.json"))
    assert err is None
    return data["devices"]


def test_update_uplinks_map_creates_map(monkeypatch, zabbix_env):
    devices = _dry_ssh_devices()
    host_id = {"ALA-KZT-7280TR-1": "101"}
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "itemid_in": "1001",
            "itemid_out": "1002",
            "bits_in": 'net.if.in["Ethernet51/1"]',
            "bits_out": 'net.if.out["Ethernet51/1"]',
        },
    }
    desc = {"Uplink: Cogent 10G": "Cogent"}

    map_created = []

    def map_get(params):
        sysmapids = params.get("sysmapids")
        if sysmapids:
            sid = str(sysmapids[0])
            return [{"sysmapid": sid, "selements": [], "links": []}]
        filt = (params.get("filter") or {}).get("name")
        if filt == MAP_NAME:
            return []
        return []

    def map_create(params):
        map_created.append(params)
        return {"sysmapids": ["55"]}

    def map_update(params):
        return True

    mocker = (
        build_standard_zabbix_mocker()
        .on("map.get", map_get)
        .on("map.create", map_create)
        .on("map.update", map_update)
    )
    mocker.activate(monkeypatch)

    err, sysmapid = update_uplinks_map(
        "https://z.example/api_jsonrpc.php",
        "token",
        devices,
        host_id,
        items,
        desc,
        debug=False,
    )
    assert err is None
    assert sysmapid == "55"
    assert map_created or True


def test_main_generate_description_map(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_map.py",
            "-f",
            str(FIXTURES / "dry_ssh_minimal.json"),
            "--generate-description-map",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "Uplink: Cogent 10G" in data


def test_main_print_table_with_zabbix(monkeypatch, zabbix_env, capsys):
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
    mocker = build_standard_zabbix_mocker(
        hosts=[
            {"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"},
            {"hostid": "102", "host": "FRN-MX-1", "name": "FRN-MX-1"},
        ],
        items=items,
    )
    mocker.activate(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_map.py",
            "-f",
            str(FIXTURES / "dry_ssh_minimal.json"),
            "--zabbix",
            "--print-table",
            "--no-cache",
        ],
    )
    main()
    out = capsys.readouterr().out
    assert "ALA-KZT-7280TR-1" in out
    assert "Ethernet51/1" in out


def test_main_create_map_only(monkeypatch, zabbix_env, capsys):
    mocker = (
        ZabbixRpcMocker()
        .on("map.get", lambda p: [])
        .on("map.create", lambda p: {"sysmapids": ["77"]})
    )
    mocker.activate(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["zabbix_map.py", "--create-map"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0


def test_main_export_map(monkeypatch, zabbix_env, capsys):
    mocker = ZabbixRpcMocker().on("map.get", lambda p: [{"sysmapid": "1", "name": "Uplinks"}])
    mocker.activate(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["zabbix_map.py", "--export-map", "1"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert "Uplinks" in capsys.readouterr().out
