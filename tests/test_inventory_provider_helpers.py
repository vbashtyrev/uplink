"""Inventory provider map helpers: case, NetBox relations, uplink filter."""
from tests.mocks.inventory_scope import minimal_inventory_netbox_interface_relations
from uplinks.netbox.inventory import device_iface_provider_map_from_inventory, expand_provider_map_for_netbox, iface_has_inventory_entry, inventory_has_provider_metadata_snapshot, inventory_provider_metadata_complete, is_uplink_iface, provider_limits_gbps_from_inventory, provider_slo_percent_from_inventory, resolve_provider_name_for_iface

def test_resolve_provider_case_insensitive():
    inventory_map = {('R1', 'ethernet51/1'): 'Cogent NetBox'}
    iface = {'name': 'Ethernet51/1', 'description': 'Transit link'}
    provider = resolve_provider_name_for_iface('R1', iface, device_iface_to_provider=inventory_map)
    assert provider == 'Cogent NetBox'

def test_expand_inventory_member_to_logical_via_netbox_relations():
    relations = minimal_inventory_netbox_interface_relations()
    inventory_map = {('FRN-MX-1', 'et-0/0/1'): 'Hurricane'}
    expanded = expand_provider_map_for_netbox(inventory_map, relations)
    assert expanded['FRN-MX-1', 'ae5.0'] == 'Hurricane'

def test_expand_inventory_aggregate_to_logical():
    relations = minimal_inventory_netbox_interface_relations()
    inventory_map = {('FRN-MX-1', 'ae5'): 'Hurricane'}
    expanded = expand_provider_map_for_netbox(inventory_map, relations)
    assert expanded['FRN-MX-1', 'ae5.0'] == 'Hurricane'

def test_is_uplink_iface_inventory_without_uplink_description():
    inventory_map = {('R1', 'ethernet51/1'): 'ManualISP'}
    iface = {'name': 'Ethernet51/1', 'description': 'Transit only'}
    assert is_uplink_iface(iface, hostname='R1', device_iface_to_provider=inventory_map) is True
    assert is_uplink_iface(iface, hostname='R1', device_iface_to_provider={}) is False

def test_is_uplink_iface_inventory_mode_ignores_uplink_description():
    inventory_map = {('WAW-EQX-7280QR-2', 'ethernet23/1'): 'Hurricane'}
    scoped = {'name': 'Ethernet23/1', 'description': 'Uplink: HurricaneE'}
    out_of_scope = {'name': 'Ethernet34/1', 'description': 'Uplink: Fiord and MSK PING-WIN 3Gbps link'}
    assert is_uplink_iface(scoped, hostname='WAW-EQX-7280QR-2', device_iface_to_provider=inventory_map) is True
    assert is_uplink_iface(out_of_scope, hostname='WAW-EQX-7280QR-2', device_iface_to_provider=inventory_map) is False

def test_is_uplink_iface_fail_closed_without_inventory():
    iface = {'name': 'Ethernet34/1', 'description': 'Uplink: Fiord'}
    assert is_uplink_iface(iface, hostname='WAW-EQX-7280QR-2') is False

def test_iface_has_inventory_entry_uses_interface_name():
    inventory_map = {('FRN-MX-1', 'ae5.0'): 'Hurricane'}
    logical = {'name': 'ae5.0', 'description': 'ISP handoff'}
    assert iface_has_inventory_entry('FRN-MX-1', logical, inventory_map) is True

def test_device_iface_map_from_inventory_normalizes_keys():
    report = {'complete': [{'provider': 'Cogent', 'device': 'R1', 'interface': 'Ethernet51/1'}]}
    mapping = device_iface_provider_map_from_inventory(report)
    assert mapping['R1', 'ethernet51/1'] == 'Cogent'

def test_device_iface_map_expands_with_netbox_relations():
    relations = {'member_to_aggregate': {('MSK-M9-MX204-2', 'et-0/0/3'): 'ae3'}, 'parent_children': {('MSK-M9-MX204-2', 'ae3'): {'ae3.0'}}, 'display_names': {('MSK-M9-MX204-2', 'et-0/0/3'): 'et-0/0/3', ('MSK-M9-MX204-2', 'ae3'): 'ae3', ('MSK-M9-MX204-2', 'ae3.0'): 'ae3.0'}}
    report = {'complete': [{'provider': 'Ertelecom', 'device': 'MSK-M9-MX204-2', 'interface': 'et-0/0/3'}]}
    mapping = device_iface_provider_map_from_inventory(report, netbox_relations=relations)
    assert mapping['MSK-M9-MX204-2', 'ae3.0'] == 'Ertelecom'
    assert mapping['MSK-M9-MX204-2', 'et-0/0/3'] == 'Ertelecom'

def test_provider_metadata_snapshot_helpers():
    report = {'provider_slo_percent': {'Cogent': 99.9}, 'provider_limits_gbps': {'Cogent': 10}, 'provider_slo_read': 'ok', 'provider_limits_read': 'ok'}
    assert inventory_provider_metadata_complete(report)
    assert inventory_has_provider_metadata_snapshot(report)
    assert provider_slo_percent_from_inventory(report)['Cogent'] == 99.9
    assert provider_limits_gbps_from_inventory(report)['Cogent'] == 10
    assert inventory_has_provider_metadata_snapshot({'complete': []}) is False
    assert inventory_provider_metadata_complete({'provider_slo_percent': {}, 'provider_limits_gbps': {}, 'provider_slo_read': 'error', 'provider_limits_read': 'ok'}) is False
