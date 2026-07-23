"""zabbix_sync_commit_rate --delete-util-triggers main path."""

import sys

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker


def test_main_delete_util_triggers_only(monkeypatch, zabbix_env, netbox_env, capsys):
    import zabbix_sync_commit_rate as mod

    build_standard_zabbix_mocker().on(
        "trigger.get",
        lambda p: [
            {
                "triggerid": "1",
                "description": "Interface Eth1: High bandwidth utilization (warning)",
                "tags": [{"tag": "scripts", "value": "automatization"}],
            },
        ],
    ).on("trigger.delete", lambda p: True).activate(monkeypatch)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(
        sys,
        "argv",
        ["zabbix_sync_commit_rate.py", "--delete-util-triggers"],
    )
    mod.main()
    assert "Deleted uplink utilization triggers" in capsys.readouterr().out
