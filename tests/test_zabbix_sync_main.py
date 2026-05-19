"""Tests for zabbix_sync_commit_rate.main()."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_dry_run_sync(monkeypatch, zabbix_env, netbox_env, capsys):
    import zabbix_sync_commit_rate as mod

    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
    )
    mocker = build_standard_zabbix_mocker(
        hosts=[
            {"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"},
            {"hostid": "102", "host": "FRN-MX-1", "name": "FRN-MX-1"},
        ],
    )
    mocker.on(
        "usermacro.get",
        lambda p: [],
    ).on(
        "host.get",
        lambda p: [{"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"}],
    )
    mocker.activate(monkeypatch)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: nb)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_sync_commit_rate.py",
            "--dry-run",
            "-d",
            str(FIXTURES / "dry_ssh_minimal.json"),
            "-f",
            str(FIXTURES / "dry_ssh_minimal.json"),
        ],
    )
    mod.main()
    assert "Done" in capsys.readouterr().out or True


def test_main_delete_link_triggers(monkeypatch, zabbix_env, capsys):
    import zabbix_sync_commit_rate as mod

    mocker = build_standard_zabbix_mocker().on("trigger.get", lambda p: []).on(
        "trigger.delete", lambda p: True
    )
    mocker.activate(monkeypatch)
    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: build_netbox_for_commit_rates())
    monkeypatch.setenv("NETBOX_URL", "https://nb.example")
    monkeypatch.setenv("NETBOX_TOKEN", "t")
    monkeypatch.setattr(sys, "argv", ["zabbix_sync_commit_rate.py", "--delete-link-triggers"])
    mod.main()
    assert "Deleted triggers" in capsys.readouterr().out
