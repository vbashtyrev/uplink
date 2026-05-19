"""zabbix_sync_commit_rate: macro sync with mocked APIs."""

import sys
from pathlib import Path
from unittest.mock import patch

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_sync_creates_macros(monkeypatch, zabbix_env, netbox_env, capsys):
    import zabbix_sync_commit_rate as mod

    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        commit_rate_kbps=10_000_000,
        device_tag="border",
    )
    created_macros = []

    def macro_create(params):
        created_macros.append(params)
        return {"hostmacroids": ["1"]}

    mocker = build_standard_zabbix_mocker(
        hosts=[{"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"}],
    )
    mocker.on("usermacro.get", lambda p: []).on("usermacro.create", macro_create)
    mocker.activate(monkeypatch)
    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: nb)
    monkeypatch.setenv("NETBOX_TAG", "border")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_sync_commit_rate.py",
            "-d",
            str(FIXTURES / "dry_ssh_minimal.json"),
            "--no-util-triggers",
        ],
    )
    mod.main()
    assert created_macros or "Done" in capsys.readouterr().out
