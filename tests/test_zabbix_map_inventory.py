"""zabbix_map provider resolution from NetBox inventory."""
from pathlib import Path
from tests.mocks.map_state import MapStateTracker
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from tests.mocks.inventory_scope import minimal_inventory_report
from uplinks.netbox.inventory import expand_provider_map_for_netbox, netbox_interface_relations_from_report, scoped_devices_from_inventory_report
from zabbix_map import MAP_NAME, update_uplinks_map
FIXTURES = Path(__file__).resolve().parent / 'fixtures'

def _single_host_map_setup(monkeypatch):
    report = minimal_inventory_report()
    devices = scoped_devices_from_inventory_report(report)
    devices = {'ALA-KZT-7280TR-1': devices['ALA-KZT-7280TR-1']}
    host_id = {'ALA-KZT-7280TR-1': '101'}
    items = {('ALA-KZT-7280TR-1', 'ethernet51/1'): {'itemid_in': '501', 'itemid_out': '502', 'bits_in': 'net.if.in["Ethernet51/1"]', 'bits_out': 'net.if.out["Ethernet51/1"]'}}
    map_state = {'sysmapid': '55', 'selements': [{'selementid': '1', 'elementtype': 0, 'elements': [{'hostid': '101'}], 'label': 'ALA-KZT-7280TR-1'}], 'links': []}
    map_tracker = MapStateTracker(sysmapid='55', selements=map_state['selements'], links=map_state['links'])
    mocker = build_standard_zabbix_mocker().on('map.get', map_tracker.map_get).on('map.create', map_tracker.map_create).on('map.update', map_tracker.map_update)
    mocker.activate(monkeypatch)
    monkeypatch.setattr('zabbix_map.get_provider_aggregate_triggers', lambda url, token, providers, debug=False: {})
    monkeypatch.setattr('zabbix_map.get_link_commit_triggers', lambda url, token, hostids, debug=False: {})
    return (devices, host_id, items, map_tracker.updates)

def test_update_map_prefers_inventory_provider(monkeypatch):
    devices, host_id, items, updates = _single_host_map_setup(monkeypatch)
    inventory_map = {('ALA-KZT-7280TR-1', 'ethernet51/1'): 'Cogent NetBox'}
    err, sid = update_uplinks_map('https://z.example/api_jsonrpc.php', 't', devices, host_id, items, device_iface_to_provider=inventory_map)
    assert err is None
    assert sid == '55'
    selement_updates = [u for u in updates if u.get('selements')]
    labels = [el.get('label') for u in selement_updates for el in u.get('selements', [])]
    assert 'Cogent NetBox' in labels
    assert 'Cogent Legacy' not in labels

def test_update_map_fallback_description_without_inventory(monkeypatch):
    devices, host_id, items, updates = _single_host_map_setup(monkeypatch)
    err, sid = update_uplinks_map('https://z.example/api_jsonrpc.php', 't', devices, host_id, items, device_iface_to_provider={})
    assert err is not None
    assert sid is None

def test_update_map_excludes_out_of_scope_uplink_description(monkeypatch):
    devices = {'WAW-EQX-7280QR-2': [{'name': 'Ethernet23/1', 'description': 'Uplink: HurricaneE'}, {'name': 'Ethernet34/1', 'description': 'Uplink: Fiord and MSK PING-WIN 3Gbps link'}]}
    host_id = {'WAW-EQX-7280QR-2': '102'}
    items = {('WAW-EQX-7280QR-2', 'ethernet23/1'): {'itemid_in': '501', 'itemid_out': '502', 'bits_in': 'net.if.in["Ethernet23/1"]', 'bits_out': 'net.if.out["Ethernet23/1"]'}, ('WAW-EQX-7280QR-2', 'ethernet34/1'): {'itemid_in': '601', 'itemid_out': '602', 'bits_in': 'net.if.in["Ethernet34/1"]', 'bits_out': 'net.if.out["Ethernet34/1"]'}}
    inventory_map = {('WAW-EQX-7280QR-2', 'ethernet23/1'): 'Hurricane'}
    map_state = {'sysmapid': '58', 'selements': [{'selementid': '1', 'elementtype': 0, 'elements': [{'hostid': '102'}], 'label': 'WAW-EQX-7280QR-2'}], 'links': []}
    map_tracker = MapStateTracker(sysmapid='58', selements=map_state['selements'], links=map_state['links'])
    build_standard_zabbix_mocker().on('map.get', map_tracker.map_get).on('map.create', map_tracker.map_create).on('map.update', map_tracker.map_update).activate(monkeypatch)
    updates = map_tracker.updates
    monkeypatch.setattr('zabbix_map.get_provider_aggregate_triggers', lambda url, token, providers, debug=False: {})
    monkeypatch.setattr('zabbix_map.get_link_commit_triggers', lambda url, token, hostids, debug=False: {})
    err, sid = update_uplinks_map('https://z.example/api_jsonrpc.php', 't', devices, host_id, items, device_iface_to_provider=inventory_map)
    assert err is None
    selement_updates = [u for u in updates if u.get('selements')]
    labels = [el.get('label') for u in selement_updates for el in u.get('selements', [])]
    assert 'Hurricane' in labels
    assert 'Fiord Legacy' not in labels
    assert 'Fiord' not in labels

def test_update_map_juniper_logical_inventory_provider(monkeypatch):
    devices = scoped_devices_from_inventory_report(minimal_inventory_report())
    devices = {'FRN-MX-1': devices['FRN-MX-1']}
    host_id = {'FRN-MX-1': '102'}
    items = {('FRN-MX-1', 'ae5.0'): {'itemid_in': '601', 'itemid_out': '602', 'bits_in': 'net.if.in["ae5.0"]', 'bits_out': 'net.if.out["ae5.0"]'}}
    inventory_map = {('FRN-MX-1', 'ae5.0'): 'Hurricane NetBox'}
    map_state = {'sysmapid': '56', 'selements': [{'selementid': '1', 'elementtype': 0, 'elements': [{'hostid': '102'}], 'label': 'FRN-MX-1'}], 'links': []}
    map_tracker = MapStateTracker(sysmapid='56', selements=map_state['selements'], links=map_state['links'])
    build_standard_zabbix_mocker().on('map.get', map_tracker.map_get).on('map.create', map_tracker.map_create).on('map.update', map_tracker.map_update).activate(monkeypatch)
    updates = map_tracker.updates
    monkeypatch.setattr('zabbix_map.get_provider_aggregate_triggers', lambda url, token, providers, debug=False: {})
    monkeypatch.setattr('zabbix_map.get_link_commit_triggers', lambda url, token, hostids, debug=False: {})
    err, sid = update_uplinks_map('https://z.example/api_jsonrpc.php', 't', devices, host_id, items, device_iface_to_provider=inventory_map)
    assert err is None
    selement_updates = [u for u in updates if u.get('selements')]
    labels = [el.get('label') for u in selement_updates for el in u.get('selements', [])]
    assert 'Hurricane NetBox' in labels

def test_update_map_inventory_without_uplink_description(monkeypatch):
    devices, host_id, items, updates = _single_host_map_setup(monkeypatch)
    devices = {'ALA-KZT-7280TR-1': [{'name': 'Ethernet51/1', 'description': 'Transit handoff only'}]}
    inventory_map = {('ALA-KZT-7280TR-1', 'ethernet51/1'): 'ManualISP'}
    err, sid = update_uplinks_map('https://z.example/api_jsonrpc.php', 't', devices, host_id, items, device_iface_to_provider=inventory_map)
    assert err is None
    selement_updates = [u for u in updates if u.get('selements')]
    labels = [el.get('label') for u in selement_updates for el in u.get('selements', [])]
    assert 'ManualISP' in labels

def test_update_map_juniper_member_inventory_to_logical(monkeypatch):
    from tests.mocks.inventory_scope import minimal_inventory_netbox_interface_relations

    devices = scoped_devices_from_inventory_report(
        minimal_inventory_report(),
        expand_provider_map_for_netbox(
            {('FRN-MX-1', 'et-0/0/1'): 'Hurricane NetBox'},
            minimal_inventory_netbox_interface_relations(),
        ),
    )
    devices = {'FRN-MX-1': devices['FRN-MX-1']}
    host_id = {'FRN-MX-1': '102'}
    items = {
        ('FRN-MX-1', 'ae5.0'): {
            'itemid_in': '601',
            'itemid_out': '602',
            'bits_in': 'net.if.in["ae5.0"]',
            'bits_out': 'net.if.out["ae5.0"]',
        }
    }
    inventory_map = expand_provider_map_for_netbox(
        {('FRN-MX-1', 'et-0/0/1'): 'Hurricane NetBox'},
        minimal_inventory_netbox_interface_relations(),
    )
    map_state = {'sysmapid': '57', 'selements': [{'selementid': '1', 'elementtype': 0, 'elements': [{'hostid': '102'}], 'label': 'FRN-MX-1'}], 'links': []}
    map_tracker = MapStateTracker(sysmapid='57', selements=map_state['selements'], links=map_state['links'])
    build_standard_zabbix_mocker().on('map.get', map_tracker.map_get).on('map.create', map_tracker.map_create).on('map.update', map_tracker.map_update).activate(monkeypatch)
    updates = map_tracker.updates
    monkeypatch.setattr('zabbix_map.get_provider_aggregate_triggers', lambda url, token, providers, debug=False: {})
    monkeypatch.setattr('zabbix_map.get_link_commit_triggers', lambda url, token, hostids, debug=False: {})
    err, sid = update_uplinks_map('https://z.example/api_jsonrpc.php', 't', devices, host_id, items, device_iface_to_provider=inventory_map)
    assert err is None
    selement_updates = [u for u in updates if u.get('selements')]
    labels = [el.get('label') for u in selement_updates for el in u.get('selements', [])]
    assert 'Hurricane NetBox' in labels
