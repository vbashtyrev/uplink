"""Inventory provider map helpers: case, Juniper aliases, uplink filter."""

from uplinks.netbox.inventory import (
    device_iface_provider_map_from_inventory,
    expand_provider_map_for_zabbix,
    iface_has_inventory_entry,
    inventory_has_provider_metadata_snapshot,
    inventory_provider_metadata_complete,
    is_uplink_iface,
    provider_limits_gbps_from_inventory,
    provider_slo_percent_from_inventory,
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
    assert is_uplink_iface(iface, hostname="R1", inventory_scoped=False) is False
    assert is_uplink_iface(
        iface, hostname="R1", device_iface_to_provider=inventory_map, inventory_scoped=True
    ) is True
    assert is_uplink_iface(iface, hostname="R1", device_iface_to_provider={}, inventory_scoped=True) is False


def test_is_uplink_iface_inventory_mode_ignores_uplink_description():
    """Out-of-scope dry-ssh iface with Uplink: must not bypass circuit scope."""
    inventory_map = {("WAW-EQX-7280QR-2", "ethernet23/1"): "Hurricane"}
    scoped = {
        "name": "Ethernet23/1",
        "description": "Uplink: HurricaneE",
    }
    out_of_scope = {
        "name": "Ethernet34/1",
        "description": "Uplink: Fiord and MSK PING-WIN 3Gbps link",
    }
    assert is_uplink_iface(
        scoped,
        hostname="WAW-EQX-7280QR-2",
        device_iface_to_provider=inventory_map,
        inventory_scoped=True,
    ) is True
    assert is_uplink_iface(
        out_of_scope,
        hostname="WAW-EQX-7280QR-2",
        device_iface_to_provider=inventory_map,
        inventory_scoped=True,
    ) is False


def test_is_uplink_iface_legacy_mode_uses_uplink_description():
    iface = {"name": "Ethernet34/1", "description": "Uplink: Fiord"}
    assert is_uplink_iface(iface, hostname="WAW-EQX-7280QR-2", inventory_scoped=False) is True


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


def test_expand_inventory_member_et003_to_ae30():
    """Inventory on physical member maps to logical ae3.0 for Zabbix."""
    dry_ssh = {
        "MSK-M9-MX204-2": [
            {"name": "ae3.0", "physicalInterface": "ae3", "isLogical": True},
            {"name": "ae3", "isLag": True},
            {"name": "et-0/0/3", "aggregateInterface": "ae3"},
        ],
    }
    inventory_map = {("MSK-M9-MX204-2", "et-0/0/3"): "Ertelecom"}
    expanded = expand_provider_map_for_zabbix(inventory_map, dry_ssh)
    assert expanded[("MSK-M9-MX204-2", "ae3.0")] == "Ertelecom"
    assert expanded[("MSK-M9-MX204-2", "et-0/0/3")] == "Ertelecom"


def test_provider_metadata_snapshot_helpers():
    report = {
        "provider_slo_percent": {"Cogent": 99.9},
        "provider_limits_gbps": {"Cogent": 10},
        "provider_slo_read": "ok",
        "provider_limits_read": "ok",
    }
    assert inventory_provider_metadata_complete(report)
    assert inventory_has_provider_metadata_snapshot(report)
    assert provider_slo_percent_from_inventory(report)["Cogent"] == 99.9
    assert provider_limits_gbps_from_inventory(report)["Cogent"] == 10
    assert inventory_has_provider_metadata_snapshot({"complete": []}) is False
    assert inventory_provider_metadata_complete(
        {
            "provider_slo_percent": {},
            "provider_limits_gbps": {},
            "provider_slo_read": "error",
            "provider_limits_read": "ok",
        }
    ) is False


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
