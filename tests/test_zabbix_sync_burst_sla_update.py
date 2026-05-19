"""ensure_burst_sla_breach_trigger update existing trigger."""

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_sync_commit_rate import (
    TRIGGER_DESC_SLA_BREACH_SUFFIX,
    ensure_burst_sla_breach_trigger,
)


def test_burst_sla_updates_existing(monkeypatch, zabbix_env):
    existing = [{
        "triggerid": "77",
        "description": "Interface Eth1: {}".format(TRIGGER_DESC_SLA_BREACH_SUFFIX),
        "priority": "0",
        "status": "1",
        "expression": "old",
    }]
    updates = []

    def item_get(params):
        if (params.get("search") or {}).get("name") == "Bits received":
            return [{"key_": 'net.if.in["Eth1"]', "name": "Interface Eth1: Bits received"}]
        return []

    mocker = build_standard_zabbix_mocker()
    mocker.on("item.get", item_get).on("trigger.get", lambda p: existing).on(
        "trigger.update", lambda p: updates.append(p) or True
    ).activate(monkeypatch)
    ok, err = ensure_burst_sla_breach_trigger(
        "https://z/api_jsonrpc.php", "t", "host1", "101", "Eth1"
    )
    assert ok is True
    assert updates
    assert any(
        u.get("expression") or u.get("priority") == "4" or "triggerid" in u
        for u in updates
    )
