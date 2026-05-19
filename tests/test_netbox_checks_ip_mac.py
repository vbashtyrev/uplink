"""netbox_checks IP/MAC/VRF helper unit tests."""

from unittest.mock import MagicMock

import netbox_checks as nc


def test_mac_and_ip_helpers():
    assert nc._normalize_mac("44:4c:a8:bf:2e:91") == "44:4c:a8:bf:2e:91"
    assert nc._mac_netbox_format("44:4c:a8:bf:2e:91") == "44:4C:A8:BF:2E:91"
    assert nc._normalize_ip_address("203.0.113.1/24") == "203.0.113.1/24"
    assert nc._is_global_routable_address("203.0.113.1/24") is True
    assert nc._is_global_routable_address("10.0.0.1/8") is False


def test_get_interface_mac_and_related_id():
    mac_rec = MagicMock()
    mac_rec.mac_address = "44:4C:A8:BF:2E:91"
    iface = MagicMock()
    iface.mac_address = "44:4C:A8:BF:2E:91"
    iface.mac_addresses = [mac_rec]
    iface.primary_mac_address = None
    assert nc._get_interface_mac(iface) == "44:4C:A8:BF:2E:91"
    assert nc._mac_both_filled(iface) is True

    parent = MagicMock()
    parent.id = 5
    iface.parent = parent
    assert nc._get_related_interface_id(iface, "parent") == 5


def test_vrf_resolve():
    nb = MagicMock()
    vrf = MagicMock()
    vrf.id = 3
    vrf.name = "internet"
    nb.ipam.vrfs.filter.return_value = [vrf]
    nb.ipam.vrfs.get.return_value = vrf
    cache = {}
    assert nc._resolve_vrf_name_to_id(nb, "internet", cache) == 3
    id_cache = {}
    assert nc._resolve_vrf_id_to_name(nb, 3, id_cache) == "internet"


def test_find_ip_helpers():
    nb = MagicMock()
    ip = MagicMock()
    ip.vrf = None
    nb.ipam.ip_addresses.filter.return_value = [ip]
    assert nc._find_ip_in_netbox(nb, "203.0.113.1/24", None) == [ip]
    assert nc._find_ip_in_netbox_any_vrf(nb, "203.0.113.1/24") == [ip]
