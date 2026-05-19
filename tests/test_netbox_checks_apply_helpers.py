"""netbox_checks apply helpers: MAC, IP, aggregate second pass."""

from unittest.mock import MagicMock

import netbox_checks as nc


def test_apply_mac_create_and_primary():
    nb = MagicMock()
    nb_iface = MagicMock()
    nb_iface.id = 7
    nb.dcim.mac_addresses.filter.return_value = []
    created = MagicMock()
    created.id = 99
    created.url = "http://nb/mac/99"
    nb.dcim.mac_addresses.create.return_value = created

    nc._apply_mac_to_interface(nb, "dev1", "Eth1", nb_iface, "44:4c:a8:bf:2e:91")
    nb.dcim.mac_addresses.create.assert_called_once()
    nb_iface.update.assert_called_with({"primary_mac_address": 99})


def test_apply_mac_move_from_other_interface():
    nb = MagicMock()
    nb_iface = MagicMock()
    nb_iface.id = 7
    rec = MagicMock()
    rec.id = 55
    rec.assigned_object_id = 3
    rec.url = "http://nb/mac/55"
    nb.dcim.mac_addresses.filter.return_value = [rec]
    old_iface = MagicMock()
    old_iface.primary_mac_address = 55
    nb.dcim.interfaces.get.return_value = old_iface

    nc._apply_mac_to_interface(nb, "dev1", "Eth1", nb_iface, "44:4c:a8:bf:2e:91")
    old_iface.update.assert_called_with({"primary_mac_address": None})
    rec.save.assert_called()


def test_apply_ip_create_and_bind():
    nb = MagicMock()
    nb_iface = MagicMock()
    nb_iface.id = 10
    nb.ipam.ip_addresses.filter.side_effect = [
        [],  # _get_interface_ip_addresses
        [],  # _find_ip_in_netbox for add
        [],  # _find_ip_in_netbox_any_vrf
    ]
    nc._apply_ip_addresses_to_interface(
        nb, "dev1", "Eth1", nb_iface, ["203.0.113.1/24"], vrf_id_f=None
    )
    nb.ipam.ip_addresses.create.assert_called_once()


def test_apply_ip_rebind_existing():
    nb = MagicMock()
    nb_iface = MagicMock()
    nb_iface.id = 10
    ip_obj = MagicMock()
    ip_obj.assigned_object_id = None
    ip_obj.vrf = None
    nb.ipam.ip_addresses.filter.side_effect = [
        [],  # current on iface
        [ip_obj],  # find for add
    ]
    nc._apply_ip_addresses_to_interface(
        nb, "dev1", "Eth1", nb_iface, ["203.0.113.1/24"], vrf_id_f=None
    )
    assert ip_obj.assigned_object_id == 10
    ip_obj.save.assert_called()


def test_aggregate_second_pass_lag():
    nb_lag = MagicMock()
    nb_lag.id = 100
    nb_member = MagicMock()
    nb_member.id = 101
    nb_member.lag = None
    nb_by_name = {"ae5": nb_lag, "et-0/0/1": nb_member}
    payload = [
        {"name": "et-0/0/1", "aggregateInterface": "ae5", "isLag": False},
    ]

    def is_physical(e, n):
        return not e.get("isLag")

    nc._apply_aggregate_relation_second_pass(
        "mx1", payload, nb_by_name, "lag", is_physical, "LAG"
    )
    nb_member.update.assert_called_with({"lag": 100})
