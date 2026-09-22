"""Dashboard by provider/location widget builders."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from zabbix_uplinks_dashboard import (
    _build_edges,
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


def _field_value(fields, name):
    for field in fields:
        if field.get("name") == name:
            return field.get("value")
    return None


def _dataset_itemids(fields, ds_idx):
    prefix = "ds.{}.itemids.".format(ds_idx)
    ids = []
    for field in fields:
        name = field.get("name", "")
        if name.startswith(prefix):
            ids.append(field["value"])
    return ids


def _capture_dashboard_create(monkeypatch):
    created = []

    def _create(payload):
        created.append(payload)
        return {"dashboardids": ["88"]}

    (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", _create)
        .activate(monkeypatch)
    )
    return created


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
    created = _capture_dashboard_create(monkeypatch)
    dash_id, err = create_dashboard_by_location(
        "https://z.example/api_jsonrpc.php",
        "t",
        _edges(),
        "Uplinks by location",
    )
    assert err is None
    assert dash_id == "88"
    pages = created[0]["pages"]
    assert len(pages) == 2
    ala_page = next(p for p in pages if p["name"] == "ALA")
    assert len(ala_page["widgets"]) == 1


def test_build_edges_keeps_two_interfaces_same_host_provider():
    devices = {
        "MSK-R1": [
            {"name": "eth1", "description": "Uplink: Piter-IX", "isLag": False},
            {"name": "eth2", "description": "Uplink: Piter-IX", "isLag": False},
        ],
    }
    items = {
        ("MSK-R1", "eth1"): {"itemid_in": "1001", "itemid_out": "1002"},
        ("MSK-R1", "eth2"): {"itemid_in": "2001", "itemid_out": "2002"},
    }
    edges = _build_edges(
        devices,
        {"MSK-R1": "101"},
        items,
        {"Uplink: Piter-IX": "Piter-IX"},
        inventory_scoped=False,
    )
    assert len(edges) == 2
    assert {e[2] for e in edges} == {"eth1", "eth2"}
    assert all(e[3] == "Piter-IX" for e in edges)


def test_create_dashboard_by_location_two_interfaces_same_host_provider(monkeypatch):
    created = _capture_dashboard_create(monkeypatch)
    edges = [
        ("MSK-R1", "101", "eth1", "Piter-IX", "1001", "1002", True, False, False),
        ("MSK-R1", "101", "eth2", "Piter-IX", "2001", "2002", True, False, False),
    ]
    dash_id, err = create_dashboard_by_location(
        "https://z.example/api_jsonrpc.php",
        "t",
        edges,
        "Uplinks by location",
    )
    assert err is None
    assert dash_id == "88"
    pages = created[0]["pages"]
    assert len(pages) == 1
    assert pages[0]["name"] == "MSK"
    assert len(pages[0]["widgets"]) == 1
    fields = pages[0]["widgets"][0]["fields"]
    assert set(_dataset_itemids(fields, 0)) == {1001, 2001}
    assert set(_dataset_itemids(fields, 1)) == {1002, 2002}


def test_create_dashboard_by_location_two_edges_same_provider_one_widget(monkeypatch):
    created = _capture_dashboard_create(monkeypatch)
    edges = [
        ("MSK-R1", "101", "eth1", "Piter-IX", "1001", "1002", True, False, False),
        ("MSK-R2", "102", "eth2", "Piter-IX", "2001", "2002", True, False, False),
    ]
    dash_id, err = create_dashboard_by_location(
        "https://z.example/api_jsonrpc.php",
        "t",
        edges,
        "Uplinks by location",
    )
    assert err is None
    assert dash_id == "88"
    pages = created[0]["pages"]
    assert len(pages) == 1
    assert pages[0]["name"] == "MSK"
    assert len(pages[0]["widgets"]) == 1
    fields = pages[0]["widgets"][0]["fields"]
    assert _field_value(fields, "ds.0.stacked") == 1
    assert _field_value(fields, "ds.1.stacked") == 1
    assert set(_dataset_itemids(fields, 0)) == {1001, 2001}
    assert set(_dataset_itemids(fields, 1)) == {1002, 2002}
    assert any(f.get("name") == "simple_triggers" for f in fields)


def test_create_dashboard_by_location_different_providers_separate_widgets(monkeypatch):
    created = _capture_dashboard_create(monkeypatch)
    edges = [
        ("MSK-R1", "101", "eth1", "Piter-IX", "1001", "1002", True, False, False),
        ("MSK-R2", "102", "eth2", "Cogent", "2001", "2002", True, False, False),
    ]
    dash_id, err = create_dashboard_by_location(
        "https://z.example/api_jsonrpc.php",
        "t",
        edges,
        "Uplinks by location",
    )
    assert err is None
    pages = created[0]["pages"]
    assert len(pages) == 1
    assert len(pages[0]["widgets"]) == 2
    titles = {w["name"] for w in pages[0]["widgets"]}
    assert titles == {"Cogent (summary)", "Piter-IX (summary)"}


def test_create_dashboard_by_location_no_items_skips_widget(monkeypatch):
    created = _capture_dashboard_create(monkeypatch)
    edges = [
        ("MSK-R1", "101", "eth1", "Piter-IX", "", "", True, False, False),
        ("MSK-R2", "102", "eth2", "Piter-IX", "2001", "2002", True, False, False),
    ]
    dash_id, err = create_dashboard_by_location(
        "https://z.example/api_jsonrpc.php",
        "t",
        edges,
        "Uplinks by location",
    )
    assert err is None
    pages = created[0]["pages"]
    assert len(pages) == 1
    assert len(pages[0]["widgets"]) == 1
    fields = pages[0]["widgets"][0]["fields"]
    assert set(_dataset_itemids(fields, 0)) == {2001}
    assert set(_dataset_itemids(fields, 1)) == {2002}


def test_create_dashboard_by_location_all_edges_without_items(monkeypatch):
    (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [])
        .activate(monkeypatch)
    )
    edges = [
        ("MSK-R1", "101", "eth1", "Piter-IX", "", "", True, False, False),
    ]
    dash_id, err = create_dashboard_by_location(
        "https://z.example/api_jsonrpc.php",
        "t",
        edges,
        "Uplinks by location",
    )
    assert dash_id is None
    assert "there are no interfaces with item In/Out in Zabbix" in err


def test_create_dashboard_by_location_show_threshold_off(monkeypatch):
    created = _capture_dashboard_create(monkeypatch)
    edges = [
        ("MSK-R1", "101", "eth1", "Piter-IX", "1001", "1002", True, False, False),
    ]
    dash_id, err = create_dashboard_by_location(
        "https://z.example/api_jsonrpc.php",
        "t",
        edges,
        "Uplinks by location",
        show_threshold=False,
    )
    assert err is None
    fields = created[0]["pages"][0]["widgets"][0]["fields"]
    assert not any(f.get("name") == "simple_triggers" for f in fields)


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
