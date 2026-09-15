"""fetch_uplink_inventory_report: project circuit scope vs border device tag."""

from unittest.mock import patch

import zabbix_sync_commit_rate as zsc
from tests.mocks.netbox_api import build_netbox_for_commit_rates
from uplinks.netbox import inventory as inv
from uplinks_config import NETBOX_MONITOR_TAG
from zabbix_sync_commit_rate import KBPS_TO_BPS, commit_rates_from_inventory_report, fetch_uplink_inventory_report


def test_fetch_uplink_inventory_report_passes_project_circuit_scope():
    nb = build_netbox_for_commit_rates(commit_rate_kbps=7000)
    scope = inv.project_circuit_scope()
    with patch.object(zsc, "collect_uplink_inventory", wraps=inv.collect_uplink_inventory) as collector:
        fetch_uplink_inventory_report(nb, tag="border", debug=False)
    collector.assert_called_once()
    assert collector.call_args.kwargs.get("tag") == "border"
    assert collector.call_args.kwargs.get("active_only") is True
    assert collector.call_args.kwargs.get("circuit_scope") == scope


def test_fetch_does_not_use_monitor_tag_as_device_tag():
    """Border devices use tag=border even when NETBOX_MONITOR_TAG is uplinks."""
    nb = build_netbox_for_commit_rates(
        device_name="ALA-R1",
        iface_name="Ethernet51/1",
        commit_rate_kbps=10000,
        device_tag="border",
        tag_device=True,
    )
    with patch.object(zsc, "collect_uplink_inventory", wraps=inv.collect_uplink_inventory) as collector:
        report = fetch_uplink_inventory_report(nb, tag="border", debug=False)
    assert collector.call_args.kwargs.get("tag") == "border"
    assert collector.call_args.kwargs.get("circuit_scope") == inv.project_circuit_scope()
    assert len(report.get("complete") or []) == 1


def test_commit_rates_with_border_tag_and_scoped_provider():
    nb = build_netbox_for_commit_rates(
        device_name="ALA-R1",
        iface_name="Ethernet51/1",
        commit_rate_kbps=10000,
        device_tag="border",
    )
    report = fetch_uplink_inventory_report(nb, tag="border", debug=False)
    result = commit_rates_from_inventory_report(report, debug=False)
    assert result == {("ALA-R1", "Ethernet51/1"): 10000 * KBPS_TO_BPS}


def test_monitor_tag_on_device_does_not_match_border_inventory():
    """Mis-set device tag (monitor tag value) yields empty inventory; border tag works."""
    nb = build_netbox_for_commit_rates(
        device_name="ALA-R1",
        iface_name="Ethernet51/1",
        commit_rate_kbps=10000,
        device_tag="border",
    )
    report_monitor = fetch_uplink_inventory_report(nb, tag=NETBOX_MONITOR_TAG, debug=False)
    report_border = fetch_uplink_inventory_report(nb, tag="border", debug=False)
    assert commit_rates_from_inventory_report(report_monitor, debug=False) == {}
    assert commit_rates_from_inventory_report(report_border, debug=False) != {}


def test_border_device_tag_maps_monitor_tag_to_border():
    from zabbix_sync_commit_rate import border_device_tag
    from uplinks.netbox.inventory import netbox_border_tag, resolve_border_device_tag

    assert border_device_tag(NETBOX_MONITOR_TAG) == "border"
    assert border_device_tag("border") == "border"
    assert border_device_tag("custom-border") == "custom-border"
    assert resolve_border_device_tag(NETBOX_MONITOR_TAG) == "border"


def test_netbox_border_tag_maps_monitor_env_to_border(monkeypatch):
    from uplinks.netbox.inventory import netbox_border_tag

    monkeypatch.setenv("NETBOX_TAG", NETBOX_MONITOR_TAG)
    assert netbox_border_tag() == "border"


def test_util_interfaces_limited_to_scoped_inventory():
    from zabbix_sync_commit_rate import util_interfaces_by_host_from_inventory

    dry_ssh = {
        "WAW-EQX-7280QR-2": [
            {"name": "Ethernet23/1", "description": "Uplink: HurricaneE"},
            {"name": "Ethernet34/1", "description": "Uplink: Fiord and MSK PING-WIN 3Gbps link"},
            {"name": "Ethernet40/1", "description": "Management"},
        ],
    }
    report = {
        "complete": [
            {
                "provider": "Hurricane",
                "device": "WAW-EQX-7280QR-2",
                "interface": "Ethernet23/1",
                "circuit_id": "Hurricane-WAW-1",
            },
        ],
    }
    util_ifaces = util_interfaces_by_host_from_inventory(dry_ssh, report)
    assert util_ifaces == {"WAW-EQX-7280QR-2": ["Ethernet23/1"]}


def test_util_interfaces_member_maps_to_physical_juniper():
    from zabbix_sync_commit_rate import util_interfaces_by_host_from_inventory

    dry_ssh = {
        "FRN-MX-1": [
            {"name": "ae5.0", "physicalInterface": "ae5", "isLogical": True},
            {"name": "ae5", "isLag": True},
            {"name": "et-0/0/1", "aggregateInterface": "ae5"},
        ],
    }
    report = {
        "complete": [
            {
                "provider": "Hurricane",
                "device": "FRN-MX-1",
                "interface": "et-0/0/1",
                "circuit_id": "CKT-1",
            },
        ],
    }
    util_ifaces = util_interfaces_by_host_from_inventory(dry_ssh, report)
    assert util_ifaces == {"FRN-MX-1": ["et-0/0/1"]}


def test_out_of_scope_circuit_excluded_even_with_border_device():
    nb = build_netbox_for_commit_rates(device_tag="border")
    circuit = nb.circuits.circuits._items[0]
    circuit.tag = "other-monitor"
    circuit.custom_fields = {"uplinks_circuit_lifecycle": "active"}
    report = fetch_uplink_inventory_report(nb, tag="border", debug=False)
    assert commit_rates_from_inventory_report(report, debug=False) == {}
