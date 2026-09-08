"""Inventory provider map helpers: case, Juniper aliases, uplink filter."""

from generate_commit_rates import is_uplink
from uplinks.netbox.inventory import (
    device_iface_provider_map_from_inventory,
    expand_provider_map_for_zabbix,
    iface_has_inventory_entry,
    is_uplink_iface,
    resolve_provider_name_for_iface,
)

JUNIPER_DRY_SSH = {
    "FRN-MX-1": [
        {"name": "ae5.0", "physicalInterface": "ae5", "isLogical": True},
        {"name": "ae5", "isLag": True},
        {"name": "et-0/0/1", "aggregateInterface": "ae5"},
    ],
}


def test_resolve_provider_case_insensitive():
    inventory_map = {("R1", "ethernet51/1"): "Cogent NetBox"}
    iface = {"name": "Ethernet51/1", "description": "Transit link"}
    provider = resolve_provider_name_for_iface(
        "R1", iface, {"Transit link": "Wrong"}, device_iface_to_provider=inventory_map
    )
    assert provider == "Cogent NetBox"


def test_expand_inventory_member_to_logical_via_aggregate():
    inventory_map = {("FRN-MX-1", "et-0/0/1"): "Hurricane"}
    expanded = expand_provider_map_for_zabbix(inventory_map, JUNIPER_DRY_SSH)
    assert expanded[("FRN-MX-1", "ae5.0")] == "Hurricane"


def test_expand_inventory_aggregate_to_logical():
    inventory_map = {("FRN-MX-1", "ae5"): "Hurricane"}
    expanded = expand_provider_map_for_zabbix(inventory_map, JUNIPER_DRY_SSH)
    assert expanded[("FRN-MX-1", "ae5.0")] == "Hurricane"


def test_expand_inventory_physical_interface_direct():
    dry_ssh = {
        "FRN-MX-1": [
            {"name": "ae5.0", "physicalInterface": "et-0/0/1", "isLogical": True},
        ],
    }
    inventory_map = {("FRN-MX-1", "et-0/0/1"): "Hurricane"}
    expanded = expand_provider_map_for_zabbix(inventory_map, dry_ssh)
    assert expanded[("FRN-MX-1", "ae5.0")] == "Hurricane"


def test_is_uplink_iface_inventory_without_uplink_description():
    inventory_map = {("R1", "ethernet51/1"): "ManualISP"}
    iface = {"name": "Ethernet51/1", "description": "Transit only"}
    assert is_uplink(iface) is False
    assert is_uplink_iface(iface, hostname="R1", device_iface_to_provider=inventory_map) is True
    assert is_uplink_iface(iface, hostname="R1", device_iface_to_provider={}) is False


def test_iface_has_inventory_via_aggregate_interface():
    inventory_map = {("FRN-MX-1", "ae5"): "Hurricane"}
    logical = {
        "name": "ae5.0",
        "physicalInterface": "ae5",
        "aggregateInterface": "ae5",
        "description": "ISP handoff",
    }
    assert iface_has_inventory_entry("FRN-MX-1", logical, inventory_map) is True


def test_device_iface_map_from_inventory_normalizes_keys():
    report = {
        "complete": [
            {"provider": "Cogent", "device": "R1", "interface": "Ethernet51/1"},
        ],
    }
    mapping = device_iface_provider_map_from_inventory(report, dry_ssh_devices=None)
    assert mapping[("R1", "ethernet51/1")] == "Cogent"


def test_expand_inventory_mixed_case_member_aggregate_physical_join():
    """Regression: inventory/dry-ssh join tolerates mixed interface name casing."""
    dry_ssh = {
        "FRN-MX-1": [
            {"name": "AE5.0", "physicalInterface": "Ae5", "isLogical": True},
            {"name": "ae5", "isLag": True},
            {"name": "ET-0/0/1", "aggregateInterface": "AE5"},
        ],
    }
    inventory_map = {("FRN-MX-1", "ET-0/0/1"): "Hurricane"}
    expanded = expand_provider_map_for_zabbix(inventory_map, dry_ssh)
    assert expanded[("FRN-MX-1", "ae5.0")] == "Hurricane"
    assert expanded[("FRN-MX-1", "et-0/0/1")] == "Hurricane"
    assert expanded[("FRN-MX-1", "ae5")] == "Hurricane"
