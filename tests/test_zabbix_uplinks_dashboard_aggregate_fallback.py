"""_get_aggregate_itemids host lookup by visible name."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import UPLINKS_AGGREGATE_HOST_PREFIX
from zabbix_uplinks_dashboard import (
    AGGREGATE_ITEM_KEY_IN,
    AGGREGATE_ITEM_KEY_OUT,
    _get_aggregate_itemids,
    create_dashboard_by_provider,
)


def test_get_aggregate_itemids_name_fallback(monkeypatch):
    visible = UPLINKS_AGGREGATE_HOST_PREFIX + "Cogent"

    def host_get(params):
        filt = params.get("filter") or {}
        if "host" in filt:
            return []
        if "name" in filt:
            return [{"hostid": "200", "host": "Cogent-Sanitized", "name": visible}]
        return []

    (
        ZabbixRpcMocker()
        .on("host.get", host_get)
        .on(
            "item.get",
            lambda p: [
                {"itemid": "501", "key_": AGGREGATE_ITEM_KEY_IN},
                {"itemid": "502", "key_": AGGREGATE_ITEM_KEY_OUT},
            ],
        )
        .activate(monkeypatch)
    )
    out = _get_aggregate_itemids("https://z.example/api_jsonrpc.php", "t", ["Cogent"])
    assert out["Cogent"] == ("501", "502")


def test_create_dashboard_by_provider_update(monkeypatch):
    edges = [
        ("ALA-1", "101", "Eth1", "Cogent", "1001", "1002", True, False, False),
        ("ALA-2", "102", "Eth2", "Cogent", "2001", "2002", True, False, False),
    ]
    updated = []
    (
        ZabbixRpcMocker()
        .on(
            "dashboard.get",
            lambda p: [{"dashboardid": "10", "name": "By provider", "pages": []}],
        )
        .on("dashboard.update", lambda p: updated.append(p) or True)
        .activate(monkeypatch)
    )
    monkeypatch.setattr(
        "zabbix_uplinks_dashboard._get_aggregate_itemids",
        lambda *a, **k: {"Cogent": ("9001", "9002")},
    )
    dash_id, err = create_dashboard_by_provider(
        "https://z.example/api_jsonrpc.php",
        "t",
        edges,
        "By provider",
        ["Cogent"],
        debug=True,
    )
    assert err is None
    assert dash_id == "10"
    assert updated
