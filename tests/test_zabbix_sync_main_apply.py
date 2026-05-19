"""zabbix_sync_commit_rate main: apply macros (not dry-run)."""

import sys
from pathlib import Path

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_apply_macros_and_util(monkeypatch, zabbix_env, netbox_env, capsys):
    import zabbix_sync_commit_rate as mod

    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        device_tag="border",
    )

    def item_get(params):
        search = (params.get("search") or {}).get("name", "")
        if search == "Bits received":
            return [{"key_": "net.if.in[1]", "name": "Interface Eth1: Bits received"}]
        if search.startswith("Interface "):
            return [
                {"key_": "net.if.in[1]"},
                {"key_": "net.if.out[1]"},
                {"key_": "net.if.speed[1]"},
            ]
        return []

    mocker = build_standard_zabbix_mocker(
        hosts=[{"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"}],
    )
    mocker.on("usermacro.get", lambda p: []).on("usermacro.create", lambda p: {"hostmacroids": ["1"]})
    mocker.on("usermacro.delete", lambda p: True)
    mocker.on("item.get", item_get).on("trigger.get", lambda p: []).on(
        "trigger.create", lambda p: {"triggerids": ["1"]}
    )
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
            "-f",
            str(FIXTURES / "dry_ssh_minimal.json"),
        ],
    )
    mod.main()
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "Done" in out or "OK" in out or "util" in out.lower()
