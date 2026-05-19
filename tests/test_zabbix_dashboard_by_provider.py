"""Dashboard by provider/location widget builders."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from zabbix_uplinks_dashboard import (
    create_dashboard_by_location,
    create_dashboard_by_provider,
    create_or_update_dashboard,
)


def _edges():
    return [
        ("ALA-R1", "101", "Eth1", "Cogent", "1001", "1002", True, False, False),
        ("ALA-R2", "102", "Eth2", "Cogent", "2001", "2002", True, False, False),
        ("FRN-MX", "103", "et-0/0/1", "Hurricane", "3001", "3002", True, False, False),
    ]


def test_create_dashboard_by_provider(monkeypatch):
    mocker = (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", lambda p: {"dashboardids": ["99"]})
        .on("item.get", lambda p: [{"itemid": "9001", "name": "Uplinks Cogent aggregate"}])
    )
    mocker.activate(monkeypatch)
    monkeypatch.setattr(
        "zabbix_uplinks_dashboard._get_aggregate_itemids",
        lambda url, token, providers, debug=False: {"Cogent": ("9001", "9002")},
    )
    dash_id, err = create_dashboard_by_provider(
        "https://z.example/api_jsonrpc.php",
        "t",
        _edges(),
        "Uplinks by provider",
        ["Cogent", "Hurricane"],
    )
    assert err is None
    assert dash_id == "99"


def test_create_dashboard_by_location(monkeypatch):
    mocker = (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", lambda p: {"dashboardids": ["88"]})
    )
    mocker.activate(monkeypatch)
    dash_id, err = create_dashboard_by_location(
        "https://z.example/api_jsonrpc.php",
        "t",
        _edges(),
        "Uplinks by location",
    )
    assert err is None
    assert dash_id == "88"


def test_create_or_update_with_threshold_off(monkeypatch):
    mocker = (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [{"dashboardid": "10", "name": "Uplinks"}])
        .on("dashboard.update", lambda p: True)
    )
    mocker.activate(monkeypatch)
    dash_id, err = create_or_update_dashboard(
        "https://z.example/api_jsonrpc.php",
        "t",
        _edges()[:2],
        "Uplinks",
        show_threshold=False,
    )
    assert err is None
    assert dash_id == "10"
