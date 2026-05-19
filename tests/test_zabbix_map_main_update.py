"""zabbix_map main --update-map branch."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_map import MAP_NAME, update_uplinks_map, load_devices_json

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_update_map(monkeypatch, zabbix_env, tmp_path, capsys):
    import zabbix_map as zm

    desc = tmp_path / "description_to_name.json"
    desc.write_text(
        '{"Uplink: Cogent 10G": "Cogent", "Uplink: Hurricane": "Hurricane"}',
        encoding="utf-8",
    )

    data, _ = load_devices_json(str(FIXTURES / "dry_ssh_minimal.json"))
    devices = data["devices"]
    host_id = {"ALA-KZT-7280TR-1": "101", "FRN-MX-1": "102"}
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "bits_in": 'net.if.in["Ethernet51/1"]',
            "bits_out": 'net.if.out["Ethernet51/1"]',
        },
        ("FRN-MX-1", "ae5.0"): {
            "bits_in": 'net.if.in[ae5]',
            "bits_out": 'net.if.out[ae5]',
        },
    }
    desc_map = {"Uplink: Cogent 10G": "Cogent", "Uplink: Hurricane": "Hurricane"}

    map_updates = []

    def map_get(params):
        sysmapids = params.get("sysmapids")
        if sysmapids:
            return [{"sysmapid": str(sysmapids[0]), "selements": [], "links": []}]
        filt = (params.get("filter") or {}).get("name")
        if filt == MAP_NAME:
            return [{"sysmapid": "42", "selements": [], "links": []}]
        return []

    mocker = (
        build_standard_zabbix_mocker()
        .on("map.get", map_get)
        .on("map.update", lambda p: map_updates.append(p) or True)
        .on("map.create", lambda p: {"sysmapids": ["42"]})
    )
    mocker.activate(monkeypatch)

    def fake_fetch(url, token, hostnames, debug=False):
        return host_id, items, None

    with patch.object(zm, "fetch_zabbix_hosts_and_items", side_effect=fake_fetch):
        with patch.object(zm, "validate_zabbix_token", lambda *a, **k: (True, None)):
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    "zabbix_map.py",
                    "-f",
                    str(FIXTURES / "dry_ssh_minimal.json"),
                    "-m",
                    str(desc),
                    "--update-map",
                    "--no-cache",
                ],
            )
            zm.main()
    captured = capsys.readouterr()
    assert map_updates or "Map updated" in captured.err
