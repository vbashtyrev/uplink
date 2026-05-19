"""Tests for zabbix_uplinks_cleanup.main()."""

import sys

from tests.mocks.zabbix_rpc import ZabbixRpcMocker


def test_main_dry_run(monkeypatch, zabbix_env, capsys):
    import zabbix_uplinks_cleanup as mod

    mocker = (
        ZabbixRpcMocker()
        .on("item.get", lambda p: [])
        .on("trigger.get", lambda p: [])
        .on("map.get", lambda p: [])
        .on("dashboard.get", lambda p: [])
    )
    mocker.activate(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["zabbix_uplinks_cleanup.py", "--dry-run"])
    mod.main()
    out = capsys.readouterr().out
    assert "dry-run" in out
