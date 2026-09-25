"""Burst trigger apply: sync dependencies with plan expectations."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import (
    TRIGGER_DESC_100_SUFFIX,
    TRIGGER_DESC_SLA_BREACH_SUFFIX,
    TRIGGER_FUNCTION_PERIOD,
)
from zabbix_sync_commit_rate import (
    TRIGGER_PRIORITY_HIGH,
    TRIGGER_PRIORITY_SLA_BREACH,
    _macro_name_for_interface,
    ensure_burst_sla_breach_trigger,
    ensure_simple_threshold_trigger,
)


def _item_handlers_eth1():
    def item_get(params):
        if (params.get("search") or {}).get("name") == "Bits received":
            return [{"key_": 'net.if.in["Eth1"]', "name": "Interface Eth1: Bits received"}]
        return []

    return item_get


def _high_expression(host_technical="host1"):
    iface = "Eth1"
    key = 'net.if.in["Eth1"]'
    macro = _macro_name_for_interface(iface)
    return "max(/{}/{}, {})>{}".format(host_technical, key, TRIGGER_FUNCTION_PERIOD, macro)


def _sla_expression(host_technical="host1"):
    from zabbix_sync_commit_rate import SLA_TRIGGER_FUNCTION_PERIOD

    iface = "Eth1"
    key = 'net.if.in["Eth1"]'
    macro = _macro_name_for_interface(iface)
    return "min(/{}/{},{})>{}".format(
        host_technical, key, SLA_TRIGGER_FUNCTION_PERIOD, macro
    )


def test_ensure_simple_threshold_clears_extra_dependencies(monkeypatch):
    desc100 = "Interface Eth1: {}".format(TRIGGER_DESC_100_SUFFIX)
    expression = _high_expression()
    updated = []
    existing = [
        {
            "triggerid": "h1",
            "description": desc100,
            "priority": str(TRIGGER_PRIORITY_HIGH),
            "status": "0",
            "expression": expression,
            "dependencies": [{"triggerid": "999"}],
        }
    ]
    (
        ZabbixRpcMocker()
        .on("item.get", _item_handlers_eth1())
        .on("trigger.get", lambda p: existing)
        .on("trigger.update", lambda p: updated.append(p) or True)
        .activate(monkeypatch)
    )
    ok, err = ensure_simple_threshold_trigger(
        "https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1"
    )
    assert ok is True
    assert err is None
    assert len(updated) == 1
    assert updated[0]["dependencies"] == []


def test_ensure_simple_threshold_update_omits_dependencies_when_empty(monkeypatch):
    desc100 = "Interface Eth1: {}".format(TRIGGER_DESC_100_SUFFIX)
    expression = _high_expression()
    updated = []
    existing = [
        {
            "triggerid": "h1",
            "description": desc100,
            "priority": str(TRIGGER_PRIORITY_HIGH),
            "status": "0",
            "expression": "stale-expression",
            "dependencies": [],
        }
    ]
    (
        ZabbixRpcMocker()
        .on("item.get", _item_handlers_eth1())
        .on("trigger.get", lambda p: existing)
        .on("trigger.update", lambda p: updated.append(p) or True)
        .activate(monkeypatch)
    )
    ok, err = ensure_simple_threshold_trigger(
        "https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1"
    )
    assert ok is True
    assert updated
    assert updated[0]["expression"] == expression
    assert "dependencies" not in updated[0]


def test_ensure_burst_sla_clears_extra_dependencies(monkeypatch):
    desc = "Interface Eth1: {}".format(TRIGGER_DESC_SLA_BREACH_SUFFIX)
    expression = _sla_expression()
    updated = []
    existing = [
        {
            "triggerid": "sla1",
            "description": desc,
            "priority": str(TRIGGER_PRIORITY_SLA_BREACH),
            "status": "0",
            "expression": expression,
            "dependencies": [{"triggerid": "888"}],
        }
    ]
    (
        ZabbixRpcMocker()
        .on("item.get", _item_handlers_eth1())
        .on("trigger.get", lambda p: existing)
        .on("trigger.update", lambda p: updated.append(p) or True)
        .activate(monkeypatch)
    )
    ok, err = ensure_burst_sla_breach_trigger(
        "https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1"
    )
    assert ok is True
    assert updated[0]["dependencies"] == []


def test_ensure_burst_sla_update_omits_dependencies_when_empty(monkeypatch):
    desc = "Interface Eth1: {}".format(TRIGGER_DESC_SLA_BREACH_SUFFIX)
    expression = _sla_expression()
    updated = []
    existing = [
        {
            "triggerid": "sla1",
            "description": desc,
            "priority": "0",
            "status": "1",
            "expression": "old",
            "dependencies": [],
        }
    ]
    (
        ZabbixRpcMocker()
        .on("item.get", _item_handlers_eth1())
        .on("trigger.get", lambda p: existing)
        .on("trigger.update", lambda p: updated.append(p) or True)
        .activate(monkeypatch)
    )
    ok, err = ensure_burst_sla_breach_trigger(
        "https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1"
    )
    assert ok is True
    assert updated
    assert updated[0]["expression"] == expression
    assert "dependencies" not in updated[0]


def test_ensure_simple_threshold_trigger_update_error_propagated(monkeypatch):
    desc100 = "Interface Eth1: {}".format(TRIGGER_DESC_100_SUFFIX)
    existing = [
        {
            "triggerid": "h1",
            "description": desc100,
            "priority": str(TRIGGER_PRIORITY_HIGH),
            "status": "0",
            "expression": "stale-expression",
            "dependencies": [],
        }
    ]

    def trigger_update(_params):
        raise RuntimeError("trigger.update failed")

    (
        ZabbixRpcMocker()
        .on("item.get", _item_handlers_eth1())
        .on("trigger.get", lambda p: existing)
        .on("trigger.update", trigger_update)
        .activate(monkeypatch)
    )
    ok, err = ensure_simple_threshold_trigger(
        "https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1"
    )
    assert ok is False
    assert err
    assert "Zabbix API" in err
