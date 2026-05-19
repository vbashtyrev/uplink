"""zabbix_sync remove_threshold_items and set_zabbix_host_if_util_macros."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from zabbix_sync_commit_rate import (
    THRESHOLD_ITEM_KEY,
    remove_threshold_items,
    set_zabbix_host_if_util_macros,
)


def test_remove_threshold_items(monkeypatch):
    deleted = []
    (
        ZabbixRpcMocker()
        .on(
            "item.get",
            lambda p: [
                {"itemid": "99", "key_": THRESHOLD_ITEM_KEY + "[Eth1]"},
            ],
        )
        .on("item.delete", lambda p: deleted.extend(p) or True)
        .activate(monkeypatch)
    )
    n, err = remove_threshold_items("https://z.example/api_jsonrpc.php", "t", "50")
    assert err is None
    assert n == 1
    assert deleted == ["99"]


def test_set_zabbix_host_if_util_macros(monkeypatch):
    (
        ZabbixRpcMocker()
        .on("usermacro.get", lambda p: [])
        .on("usermacro.create", lambda p: {"hostmacroids": ["1", "2"]})
        .activate(monkeypatch)
    )
    macros = [
        {"macro": "{$UPLINK.BPS.MAX}", "value": "1", "type": "0"},
        {"macro": "{$UPLINK.BPS.WARN}", "value": "2", "type": "0"},
    ]
    ok, err = set_zabbix_host_if_util_macros(
        "https://z.example/api_jsonrpc.php", "t", "50", macros
    )
    assert ok is True
    assert err is None
