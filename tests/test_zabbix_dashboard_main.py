"""Tests for zabbix_uplinks_dashboard.main()."""

import sys
from pathlib import Path

import pytest

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_updates_dashboard(monkeypatch, zabbix_env, capsys):
    import zabbix_uplinks_dashboard as mod

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
    ).on("dashboard.get", lambda p: []).on(
        "dashboard.create",
        lambda p: {"dashboardids": ["55"]},
    )
    mocker.activate(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_uplinks_dashboard.py",
            "-f",
            str(FIXTURES / "dry_ssh_minimal.json"),
            "--no-cache",
            "--dashboard-by-location",
            "",
            "--dashboard-by-provider",
            "",
        ],
    )
    mod.main()
    assert "dashboard" in capsys.readouterr().out.lower()
