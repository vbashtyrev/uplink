"""netbox_checks VRF resolution and IP listing."""

from unittest.mock import MagicMock, patch

import netbox_checks as nc


def test_resolve_vrf_name_and_id():
    nb = MagicMock()
    nb.ipam.vrfs.filter.return_value = [type("V", (), {"id": 7, "name": "internet"})()]
    cache = {}
    assert nc._resolve_vrf_name_to_id(nb, "internet", cache) == 7
    assert cache["internet"] == 7
    id_cache = {}
    nb.ipam.vrfs.get = MagicMock(return_value=type("V", (), {"id": 7, "name": "internet"})())
    assert nc._resolve_vrf_id_to_name(nb, 7, id_cache) == "internet"


def test_get_interface_ip_addresses_filters_private():
    nb = MagicMock()
    nb_iface = MagicMock(id=1)
    nb.ipam.ip_addresses.filter.return_value = [
        MagicMock(address="10.0.0.1/24", vrf=None),
        MagicMock(address="203.0.113.1/24", vrf=None),
    ]
    addrs = nc._get_interface_ip_addresses(nb, nb_iface)
    assert any("203.0.113" in a for a, _ in addrs)
    assert not any(a.startswith("10.") for a, _ in addrs)
