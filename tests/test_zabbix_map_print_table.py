"""zabbix_map --print-table --zabbix with cache debug."""

import sys
from pathlib import Path
from unittest.mock import patch

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_map import ZABBIX_CACHE_FILE, load_devices_json, save_zabbix_cache

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_print_table_with_cache(monkeypatch, zabbix_env, tmp_path, capsys):
    import zabbix_map as zm

    desc = tmp_path / "description_to_name.json"
    desc.write_text(
        '{"Uplink: Cogent 10G": "Cogent", "Uplink: Hurricane": "Hurricane"}',
        encoding="utf-8",
    )
    dry = tmp_path / "dry.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    cache = tmp_path / ZABBIX_CACHE_FILE
    save_zabbix_cache(
        str(cache),
        {"ALA-KZT-7280TR-1": "101", "FRN-MX-1": "102"},
        {
            ("ALA-KZT-7280TR-1", "Ethernet51/1"): {
                "bits_in": 'net.if.in["Ethernet51/1"]',
                "bits_out": 'net.if.out["Ethernet51/1"]',
            },
            ("FRN-MX-1", "ae5.0"): {
                "bits_in": 'net.if.in[ae5]',
                "bits_out": 'net.if.out[ae5]',
            },
        },
    )

    build_standard_zabbix_mocker().activate(monkeypatch)
    monkeypatch.setattr(zm, "validate_zabbix_token", lambda *a, **k: (True, None))

    def fake_fetch(url, token, hostnames, debug=False):
        return (
            {"ALA-KZT-7280TR-1": "101", "FRN-MX-1": "102"},
            {
                ("ALA-KZT-7280TR-1", "Ethernet51/1"): {
                    "bits_in": 'net.if.in["Ethernet51/1"]',
                    "bits_out": 'net.if.out["Ethernet51/1"]',
                },
                ("FRN-MX-1", "ae5.0"): {
                    "bits_in": 'net.if.in[ae5]',
                    "bits_out": 'net.if.out[ae5]',
                },
            },
            None,
        )

    with patch.object(zm, "fetch_zabbix_hosts_and_items", side_effect=fake_fetch):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "zabbix_map.py",
                "-f",
                str(dry),
                "-m",
                str(desc),
                "--print-table",
                "--zabbix",
                "--debug",
                "--no-cache",
            ],
        )
        zm.main()
    out = capsys.readouterr().out
    assert "hostname" in out or "ALA-KZT" in out
