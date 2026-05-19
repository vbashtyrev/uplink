"""zabbix_uplinks_dashboard: by-provider dashboard and aggregate items."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import UPLINKS_AGGREGATE_HOST_PREFIX
from zabbix_uplinks_dashboard import (
    AGGREGATE_ITEM_KEY_IN,
    AGGREGATE_ITEM_KEY_OUT,
    _get_aggregate_itemids,
    create_dashboard_by_location,
    create_dashboard_by_provider,
)

FIXTURES = __import__("pathlib").Path(__file__).resolve().parent / "fixtures"


def _edges_two_cogent():
    return [
        ("ALA-1", "101", "eth1", "Cogent", "1001", "1002", "ki", "ko", "d1"),
        ("ALA-2", "102", "eth2", "Cogent", "1003", "1004", "ki", "ko", "d2"),
    ]


def test_get_aggregate_itemids(monkeypatch):
    agg_host = UPLINKS_AGGREGATE_HOST_PREFIX + "Cogent"
    (
        ZabbixRpcMocker()
        .on(
            "host.get",
            lambda p: [{"hostid": "200", "host": agg_host, "name": agg_host}],
        )
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
    assert out["Cogent"][0] == "501"
    assert out["Cogent"][1] == "502"


def test_create_dashboard_by_provider(monkeypatch):
    created = []
    (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", lambda p: created.append(p) or {"dashboardids": ["77"]})
        .on(
            "host.get",
            lambda p: [
                {
                    "hostid": "200",
                    "host": UPLINKS_AGGREGATE_HOST_PREFIX + "Cogent",
                    "name": UPLINKS_AGGREGATE_HOST_PREFIX + "Cogent",
                },
            ],
        )
        .on(
            "item.get",
            lambda p: [
                {"itemid": "501", "key_": AGGREGATE_ITEM_KEY_IN},
                {"itemid": "502", "key_": AGGREGATE_ITEM_KEY_OUT},
            ],
        )
        .activate(monkeypatch)
    )
    did, err = create_dashboard_by_provider(
        "https://z.example/api_jsonrpc.php",
        "t",
        _edges_two_cogent(),
        "Uplinks by provider",
        ["Cogent"],
    )
    assert err is None
    assert did == "77"
    assert created and created[0]["pages"]


def test_create_dashboard_by_location_update(monkeypatch):
    updated = []
    (
        ZabbixRpcMocker()
        .on(
            "dashboard.get",
            lambda p: [{"dashboardid": "5", "name": "By location", "pages": []}],
        )
        .on("dashboard.update", lambda p: updated.append(p) or True)
        .activate(monkeypatch)
    )
    edges = [
        ("ALA-KZT-7280TR-1", "101", "eth1", "Cogent", "1001", "1002", "ki", "ko", "d"),
    ]
    did, err = create_dashboard_by_location(
        "https://z.example/api_jsonrpc.php", "t", edges, "By location"
    )
    assert err is None
    assert did == "5"
    assert updated

