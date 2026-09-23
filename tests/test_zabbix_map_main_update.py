"""zabbix_map main --update-map branch."""

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

    map_tracker = MapStateTracker(sysmapid="42")

    mocker = (
        build_standard_zabbix_mocker()
        .on("map.get", map_tracker.map_get)
        .on("map.update", map_tracker.map_update)
        .on("map.create", map_tracker.map_create)
    )
    mocker.activate(monkeypatch)
    map_updates = map_tracker.updates

    def fake_fetch(url, token, hostnames, debug=False):
        return host_id, items, None

    with patch.object(zm, "fetch_zabbix_hosts_and_items", side_effect=fake_fetch):
        with patch.object(zm, "validate_zabbix_token", lambda *a, **k: (True, None)):
            with patch.object(
                zm, "load_uplink_provider_context", return_value=dry_ssh_minimal_inventory_context()
            ):
                inv = write_dry_ssh_minimal_inventory(tmp_path)
                monkeypatch.setattr(
                    sys,
                    "argv",
                    [
                        "zabbix_map.py",
                        "--inventory-file",
                        str(inv),
                        "-m",
                        str(desc),
                        "--update-map",
                        "--no-cache",
                    ],
                )
                zm.main()
    captured = capsys.readouterr()
    assert map_updates or "Map updated" in captured.err
