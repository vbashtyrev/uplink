"""netbox_checks _apply_ip_addresses_to_interface and _apply_mac_to_interface."""

from unittest.mock import MagicMock, patch

import netbox_checks as nc


def test_apply_ip_create(capsys):
    nb = MagicMock()
    nb_iface = MagicMock(id=10)
    nb.ipam.ip_addresses.create = MagicMock()
    with patch.object(nc, "_get_interface_ip_addresses", return_value=[]):
        with patch.object(nc, "_find_ip_in_netbox", return_value=None):
            with patch.object(nc, "_find_ip_in_netbox_any_vrf", return_value=None):
                nc._apply_ip_addresses_to_interface(
                    nb, "R1", "Eth1", nb_iface, ["203.0.113.2/24"], vrf_id_f=None
                )
    nb.ipam.ip_addresses.create.assert_called_once()
    assert "created" in capsys.readouterr().out


def test_apply_ip_unlink_existing(capsys):
    nb = MagicMock()
    nb_iface = MagicMock(id=10)
    existing_ip = MagicMock()
    existing_ip.assigned_object_id = 10
    existing_ip.assigned_object_type = "dcim.interface"
    existing_ip.save = MagicMock()
    with patch.object(nc, "_get_interface_ip_addresses", return_value=[("203.0.113.9/24", None)]):
        with patch.object(nc, "_find_ip_in_netbox", return_value=[existing_ip]):
            nc._apply_ip_addresses_to_interface(nb, "R1", "Eth1", nb_iface, [], vrf_id_f=None)
    existing_ip.save.assert_called()


def test_apply_mac_reassign_from_old_interface(capsys):
    nb = MagicMock()
    nb_iface = MagicMock(id=20)
    nb_iface.update = MagicMock()
    old_iface = MagicMock(id=5, primary_mac_address=99)
    old_iface.update = MagicMock()
    mac_rec = MagicMock(id=99, assigned_object_id=5)
    mac_rec.save = MagicMock()
    nb.dcim.mac_addresses.filter.return_value = [mac_rec]
    nb.dcim.interfaces.get.side_effect = lambda pk: old_iface if pk == 5 else nb_iface

    nc._apply_mac_to_interface(nb, "R1", "Eth1", nb_iface, "44:4C:A8:BF:2E:91")
    old_iface.update.assert_called_with({"primary_mac_address": None})
    assert mac_rec.assigned_object_id == 20
    nb_iface.update.assert_called_with({"primary_mac_address": 99})
