"""zabbix_map main when map already exists (default create path)."""

import sys
from pathlib import Path
from unittest.mock import patch

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_map import MAP_NAME, main as map_main

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_default_map_already_exists(monkeypatch, zabbix_env, capsys):
    host_id = {"ALA-KZT-7280TR-1": "101", "FRN-MX-1": "102"}
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {"bits_in": "in", "bits_out": "out"},
        ("FRN-MX-1", "ae5.0"): {"bits_in": "in", "bits_out": "out"},
    }

    def map_get(params):
        if (params.get("filter") or {}).get("name") == MAP_NAME:
            return [{"sysmapid": "42"}]
        return []

    build_standard_zabbix_mocker().on("map.get", map_get).activate(monkeypatch)
    monkeypatch.setattr("zabbix_map.fetch_zabbix_hosts_and_items", lambda *a, **k: (host_id, items, None))
    monkeypatch.setattr(
        sys,
        "argv",
        ["zabbix_map.py", "-f", str(FIXTURES / "dry_ssh_minimal.json"), "--no-cache"],
    )
    map_main()
    err = capsys.readouterr().err
    assert "already exists" in err.lower() or "42" in err
