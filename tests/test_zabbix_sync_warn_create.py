"""ensure_simple_warn_trigger create path with high trigger dependency."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import TRIGGER_DESC_100_SUFFIX, TRIGGER_DESC_90_SUFFIX
from zabbix_sync_commit_rate import ensure_simple_warn_trigger


def _item_get(params):
    search = (params.get("search") or {}).get("name", "")
    if search == "Bits received":
        return [{"key_": "net.if.in[1]", "name": "Interface Eth1: Bits received"}]
    return []


def test_ensure_simple_warn_trigger_creates_with_dependency(monkeypatch):
    desc90 = "Interface Eth1: {}".format(TRIGGER_DESC_90_SUFFIX)
    desc100 = "Interface Eth1: {}".format(TRIGGER_DESC_100_SUFFIX)
    created = []

    def trigger_get(params):
        search = (params.get("search") or {}).get("description", "")
        if params.get("output") == ["triggerid", "description"]:
            return [{"triggerid": "high1", "description": desc100}]
        return []

    (
        ZabbixRpcMocker()
        .on("item.get", _item_get)
        .on("trigger.get", trigger_get)
        .on("trigger.create", lambda p: created.append(p) or {"triggerids": ["w1"]})
        .activate(monkeypatch)
    )

    ok, err = ensure_simple_warn_trigger(
        "https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1"
    )
    assert ok is True
    assert err is None
    assert created
    assert created[0].get("dependencies") == [{"triggerid": "high1"}]
