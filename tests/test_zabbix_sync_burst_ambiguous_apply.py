"""Regression: ambiguous Burst trigger matches must fail closed on apply."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import TRIGGER_DESC_100_SUFFIX
from zabbix_sync_commit_rate import ensure_simple_threshold_trigger


def _item_get(params):
    search = (params.get("search") or {}).get("name", "")
    if search == "Bits received":
        return [{"key_": "net.if.in[1]", "name": "Interface Eth1(x): Bits received"}]
    return []


def test_ensure_simple_threshold_trigger_ambiguous_duplicate(monkeypatch):
    desc100 = "Interface Eth1: {}".format(TRIGGER_DESC_100_SUFFIX)
    duplicate_triggers = [
        {
            "triggerid": "100",
            "description": desc100,
            "priority": "4",
            "status": "0",
            "expression": "old-a",
        },
        {
            "triggerid": "101",
            "description": desc100,
            "priority": "4",
            "status": "0",
            "expression": "old-b",
        },
    ]
    writes = {"update": [], "create": []}

    (
        ZabbixRpcMocker()
        .on("item.get", _item_get)
        .on("trigger.get", lambda p: duplicate_triggers)
        .on("trigger.update", lambda p: writes["update"].append(p) or True)
        .on("trigger.create", lambda p: writes["create"].append(p) or {"triggerids": ["999"]})
        .activate(monkeypatch)
    )

    ok, err = ensure_simple_threshold_trigger(
        "https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1"
    )

    assert ok is False
    assert err is not None
    assert "ambiguous burst trigger description match" in err
    assert writes["update"] == []
    assert writes["create"] == []
