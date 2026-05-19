"""zabbix_uplinks_dashboard create_or_update_dashboard with mocked API."""

from zabbix_uplinks_dashboard import create_or_update_dashboard, create_dashboard_by_location
from tests.mocks.zabbix_rpc import ZabbixRpcMocker


def _sample_edges():
    return [
        ("ALA-R1", "101", "Ethernet51/1", "Cogent", "1001", "1002", True, False, False),
        ("ALA-R2", "102", "Ethernet52/1", "Cogent", "2001", "2002", True, False, False),
    ]


def test_create_or_update_dashboard_create(monkeypatch):
    created = []

    mocker = (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", lambda p: created.append(p) or {"dashboardids": ["55"]})
    )
    mocker.activate(monkeypatch)

    dash_id, err = create_or_update_dashboard(
        "https://z.example/api_jsonrpc.php",
        "t",
        _sample_edges(),
        "Uplinks Test",
    )
    assert err is None
    assert dash_id == "55"
    assert created
    assert len(created[0]["pages"][0]["widgets"]) == 2


def test_create_or_update_dashboard_update(monkeypatch):
    updated = []
    mocker = (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [{"dashboardid": "10", "name": "Uplinks Test"}])
        .on("dashboard.update", lambda p: updated.append(p) or True)
    )
    mocker.activate(monkeypatch)

    dash_id, err = create_or_update_dashboard(
        "https://z.example/api_jsonrpc.php",
        "t",
        _sample_edges(),
        "Uplinks Test",
    )
    assert err is None
    assert dash_id == "10"
    assert updated


def test_create_dashboard_by_location(monkeypatch):
    mocker = (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", lambda p: {"dashboardids": ["77"]})
    )
    mocker.activate(monkeypatch)

    dash_id, err = create_dashboard_by_location(
        "https://z.example/api_jsonrpc.php",
        "t",
        _sample_edges(),
        "Uplinks by location",
    )
    assert err is None
    assert dash_id == "77"
