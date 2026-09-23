"""zabbix_map main: default create map when missing."""

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
from zabbix_map import MAP_NAME, main as map_main

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_default_creates_map_when_absent(monkeypatch, zabbix_env, tmp_path, capsys):
    map_tracker = MapStateTracker(sysmapid="55", exists=False)

    mocker = build_standard_zabbix_mocker()
    mocker.on("map.get", map_tracker.map_get).on("map.create", map_tracker.map_create)
    mocker.on("map.update", map_tracker.map_update).activate(monkeypatch)
    created = map_tracker.updates
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
    inv = write_dry_ssh_minimal_inventory(tmp_path)
    with patch("zabbix_map.load_uplink_provider_context", return_value=dry_ssh_minimal_inventory_context()):
        monkeypatch.setattr(
            sys,
            "argv",
            ["zabbix_map.py", "--inventory-file", str(inv), "--no-cache"],
        )
        map_main()
    err = capsys.readouterr().err
    assert "created" in err.lower() or "sysmapid" in err
