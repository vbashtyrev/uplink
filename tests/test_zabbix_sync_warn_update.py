"""ensure_simple_warn_trigger update path with 100% dependency."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import TRIGGER_DESC_100_SUFFIX, TRIGGER_DESC_90_SUFFIX
from zabbix_sync_commit_rate import (
    TRIGGER_FUNCTION_PERIOD,
    TRIGGER_PRIORITY_WARN,
    _macro_name_warn_for_interface,
    ensure_simple_warn_trigger,
)


def _warn_expression(host_technical="host1", iface="Eth1", key="net.if.in[1]"):
    macro = _macro_name_warn_for_interface(iface)
    return "max(/{}/{}, {})>{}".format(host_technical, key, TRIGGER_FUNCTION_PERIOD, macro)


def _item_get(params):
    search = (params.get("search") or {}).get("name", "")
    if search == "Bits received":
        return [{"key_": "net.if.in[1]", "name": "Interface Eth1(x): Bits received"}]
    return []


def test_ensure_simple_warn_trigger_updates_existing(monkeypatch):
    desc90 = "Interface Eth1: {}".format(TRIGGER_DESC_90_SUFFIX)
    desc100 = "Interface Eth1: {}".format(TRIGGER_DESC_100_SUFFIX)
    updated = []

    def trigger_get(params):
        search = (params.get("search") or {}).get("description", "")
        out = params.get("output") or []
        if "Interface Eth1:" in str(search):
            if out == ["triggerid", "description"]:
                return [{"triggerid": "high1", "description": desc100}]
            return [
                {
                    "triggerid": "warn1",
                    "description": desc90,
                    "status": "1",
                    "expression": "old",
                    "priority": "4",
                    "dependencies": [],
                },
            ]
        return []

    (
        ZabbixRpcMocker()
        .on("item.get", _item_get)
        .on("trigger.get", trigger_get)
        .on("trigger.update", lambda p: updated.append(p) or True)
        .activate(monkeypatch)
    )

    ok, err = ensure_simple_warn_trigger(
        "https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1"
    )
    assert ok is True
    assert err is None
    assert updated
    assert updated[0].get("dependencies") == [{"triggerid": "high1"}]
    assert updated[0].get("priority") == TRIGGER_PRIORITY_WARN


def test_ensure_simple_warn_trigger_update_high_lookup_error(monkeypatch):
    desc90 = "Interface Eth1: {}".format(TRIGGER_DESC_90_SUFFIX)
    updated = []

    def trigger_get(params):
        out = params.get("output") or []
        if "Interface Eth1:" in str((params.get("search") or {}).get("description", "")):
            if out == ["triggerid", "description"]:
                raise RuntimeError("trigger.get high failed")
            return [
                {
                    "triggerid": "warn1",
                    "description": desc90,
                    "status": "1",
                    "expression": "old",
                    "priority": "4",
                    "dependencies": [],
                },
            ]
        return []

    (
        ZabbixRpcMocker()
        .on("item.get", _item_get)
        .on("trigger.get", trigger_get)
        .on("trigger.update", lambda p: updated.append(p) or True)
        .activate(monkeypatch)
    )

    ok, err = ensure_simple_warn_trigger(
        "https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1"
    )
    assert ok is False
    assert err
    assert not updated


def test_ensure_simple_warn_trigger_update_missing_high(monkeypatch):
    desc90 = "Interface Eth1: {}".format(TRIGGER_DESC_90_SUFFIX)
    updated = []

    def trigger_get(params):
        out = params.get("output") or []
        if "Interface Eth1:" in str((params.get("search") or {}).get("description", "")):
            if out == ["triggerid", "description"]:
                return []
            return [
                {
                    "triggerid": "warn1",
                    "description": desc90,
                    "status": "1",
                    "expression": "old",
                    "priority": "4",
                    "dependencies": [],
                },
            ]
        return []

    (
        ZabbixRpcMocker()
        .on("item.get", _item_get)
        .on("trigger.get", trigger_get)
        .on("trigger.update", lambda p: updated.append(p) or True)
        .activate(monkeypatch)
    )

    ok, err = ensure_simple_warn_trigger(
        "https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1"
    )
    assert ok is False
    assert err
    assert "100% burst trigger was not found" in err
    assert not updated


def test_ensure_simple_warn_trigger_normalizes_duplicate_dependencies(monkeypatch):
    desc90 = "Interface Eth1: {}".format(TRIGGER_DESC_90_SUFFIX)
    desc100 = "Interface Eth1: {}".format(TRIGGER_DESC_100_SUFFIX)
    expression = _warn_expression()
    updated = []

    def trigger_get(params):
        out = params.get("output") or []
        if "Interface Eth1:" in str((params.get("search") or {}).get("description", "")):
            if out == ["triggerid", "description"]:
                return [{"triggerid": "high1", "description": desc100}]
            return [
                {
                    "triggerid": "warn1",
                    "description": desc90,
                    "status": "0",
                    "expression": expression,
                    "priority": str(TRIGGER_PRIORITY_WARN),
                    "dependencies": [
                        {"triggerid": "high1"},
                        {"triggerid": "high1"},
                    ],
                },
            ]
        return []

    (
        ZabbixRpcMocker()
        .on("item.get", _item_get)
        .on("trigger.get", trigger_get)
        .on("trigger.update", lambda p: updated.append(p) or True)
        .activate(monkeypatch)
    )

    ok, err = ensure_simple_warn_trigger(
        "https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1"
    )
    assert ok is True
    assert err is None
    assert len(updated) == 1
    assert updated[0].get("dependencies") == [{"triggerid": "high1"}]
    assert "expression" not in updated[0]


def test_ensure_simple_warn_trigger_single_dependency_skips_dependency_update(monkeypatch):
    desc90 = "Interface Eth1: {}".format(TRIGGER_DESC_90_SUFFIX)
    desc100 = "Interface Eth1: {}".format(TRIGGER_DESC_100_SUFFIX)
    expression = _warn_expression()
    updated = []

    def trigger_get(params):
        out = params.get("output") or []
        if "Interface Eth1:" in str((params.get("search") or {}).get("description", "")):
            if out == ["triggerid", "description"]:
                return [{"triggerid": "high1", "description": desc100}]
            return [
                {
                    "triggerid": "warn1",
                    "description": desc90,
                    "status": "0",
                    "expression": expression,
                    "priority": str(TRIGGER_PRIORITY_WARN),
                    "dependencies": [{"triggerid": "high1"}],
                },
            ]
        return []

    (
        ZabbixRpcMocker()
        .on("item.get", _item_get)
        .on("trigger.get", trigger_get)
        .on("trigger.update", lambda p: updated.append(p) or True)
        .activate(monkeypatch)
    )

    ok, err = ensure_simple_warn_trigger(
        "https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1"
    )
    assert ok is True
    assert err is None
    assert not updated
