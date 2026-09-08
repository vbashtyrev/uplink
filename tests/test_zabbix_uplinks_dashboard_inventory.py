"""zabbix_uplinks_dashboard provider list and edges from NetBox inventory."""

from unittest.mock import patch

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import PROVIDERS_FOR_SUMMARY
from zabbix_uplinks_dashboard import (
    _build_edges,
    _get_providers_from_inventory,
    create_dashboard_by_provider,
)


def test_build_edges_prefers_inventory_over_description_map():
    devices = {
        "ALA-KZT-7280TR-1": [
            {"name": "Ethernet51/1", "description": "Uplink: Cogent 10G"},
        ],
    }
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "itemid_in": "501",
            "itemid_out": "502",
        },
    }
    inventory_map = {("ALA-KZT-7280TR-1", "ethernet51/1"): "Cogent NetBox"}
    edges = _build_edges(
        devices,
        {"ALA-KZT-7280TR-1": "101"},
        items,
        {"Uplink: Cogent 10G": "Cogent Legacy"},
        device_iface_to_provider=inventory_map,
    )
    assert len(edges) == 1
    assert edges[0][3] == "Cogent NetBox"


def test_build_edges_fallback_description_without_inventory():
    devices = {
        "ALA-KZT-7280TR-1": [
            {"name": "Ethernet51/1", "description": "Uplink: Cogent 10G"},
        ],
    }
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "itemid_in": "501",
            "itemid_out": "502",
        },
    }
    edges = _build_edges(
        devices,
        {"ALA-KZT-7280TR-1": "101"},
        items,
        {"Uplink: Cogent 10G": "Cogent Legacy"},
        device_iface_to_provider={},
    )
    assert edges[0][3] == "Cogent Legacy"


def test_build_edges_inventory_without_uplink_description():
    devices = {
        "ALA-KZT-7280TR-1": [
            {"name": "Ethernet51/1", "description": "Transit handoff only"},
        ],
    }
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "itemid_in": "501",
            "itemid_out": "502",
        },
    }
    inventory_map = {("ALA-KZT-7280TR-1", "ethernet51/1"): "ManualISP"}
    edges = _build_edges(
        devices,
        {"ALA-KZT-7280TR-1": "101"},
        items,
        {},
        device_iface_to_provider=inventory_map,
    )
    assert len(edges) == 1
    assert edges[0][3] == "ManualISP"


def test_build_edges_skips_non_uplink_without_inventory():
    devices = {
        "ALA-KZT-7280TR-1": [
            {"name": "Ethernet52/1", "description": "Management link"},
        ],
    }
    edges = _build_edges(
        devices,
        {"ALA-KZT-7280TR-1": "101"},
        {},
        {},
        device_iface_to_provider={},
    )
    assert edges == []


def test_get_providers_from_inventory_manual_without_automatization(monkeypatch):
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        device_tag="border",
        tag_device=True,
    )
    dry_ssh = {
        "ALA-KZT-7280TR-1": [
            {"name": "Ethernet51/1", "description": "Uplink: Manual"},
        ],
    }
    with patch("uplinks.netbox.inventory.pynetbox.api", return_value=nb):
        monkeypatch.setenv("NETBOX_URL", "https://nb.example")
        monkeypatch.setenv("NETBOX_TOKEN", "tok")
        monkeypatch.setenv("NETBOX_TAG", "border")
        names = _get_providers_from_inventory(dry_ssh, debug=False)
    assert names == ["ManualISP"]


def test_dashboard_by_provider_includes_manual_inventory_provider(monkeypatch):
    edges = [
        ("ALA-KZT-7280TR-1", "101", "Ethernet51/1", "ManualISP", "501", "502", True, False, False),
    ]
    created = []

    (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", lambda p: created.append(p) or {"dashboardids": ["77"]})
        .activate(monkeypatch)
    )
    monkeypatch.setattr(
        "zabbix_uplinks_dashboard._get_aggregate_itemids",
        lambda url, token, providers, debug=False: {},
    )

    dash_id, err = create_dashboard_by_provider(
        "https://z.example/api_jsonrpc.php",
        "t",
        edges,
        "Uplinks by provider",
        list(PROVIDERS_FOR_SUMMARY) + ["ManualISP"],
    )
    assert err is None
    assert dash_id == "77"
    assert created
    page_names = [page.get("name") for page in created[0].get("pages", [])]
    assert "ManualISP" in page_names
