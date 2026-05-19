"""ensure_simple_warn_trigger update path with 100% dependency."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import TRIGGER_DESC_100_SUFFIX, TRIGGER_DESC_90_SUFFIX
from zabbix_sync_commit_rate import ensure_simple_warn_trigger


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
        if "Interface Eth1:" in str(search):
            if params.get("output") == ["triggerid", "description"]:
                return [{"triggerid": "high1", "description": desc100}]
            return [
                {
                    "triggerid": "warn1",
                    "description": desc90,
                    "status": "1",
                    "expression": "old",
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
