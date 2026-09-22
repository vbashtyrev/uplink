"""netbox_checks.py helpers and --apply MAC path with mocked NetBox."""

from unittest.mock import MagicMock, patch

import pytest

from netbox_checks import (
    _apply_ip_addresses_to_interface,
    _apply_mac_to_interface,
    _fwd_file_to_netbox_mode,
    _get_interface_mac,
    _mac_both_filled,
    _mac_netbox_format,
    _mt_in_ref,
    _mt_to_value,
    _netbox_type_to_str,
    _normalize_duplex,
    _normalize_mac,
    load_mt_ref,
)


def test_normalize_duplex_and_fwd_mode():
    assert _normalize_duplex("duplexFull") == "full"
    assert _normalize_duplex("half") == "half"
    assert _fwd_file_to_netbox_mode("routed") is None
    assert _fwd_file_to_netbox_mode("bridged") == "tagged"


def test_mac_normalization():
    assert _normalize_mac("44-4c-a8-bf-2e-91") == "44:4c:a8:bf:2e:91"
    assert _mac_netbox_format("44:4c:a8:bf:2e:91") == "44:4C:A8:BF:2E:91"


def test_load_mt_ref_and_mt_to_value(tmp_path):
    ref_file = tmp_path / "types.json"
    ref_file.write_text(
        '{"interface_types": [{"value": "10gbase-x-sfpp", "label": "SFP+ (10GE)"}]}',
        encoding="utf-8",
    )
    values, ref_list, err = load_mt_ref(str(ref_file))
    assert err is None
    assert "10gbase-x-sfpp" in values
    assert _mt_to_value("SFP+ (10GE)", values, ref_list) == "10gbase-x-sfpp"
    assert _mt_in_ref("10gbase-x-sfpp", values, ref_list) is True


def test_netbox_type_to_str():
    iface = MagicMock()
    iface.type = {"value": "virtual", "label": "Virtual"}
    assert _netbox_type_to_str(iface) == "virtual"


def test_get_interface_mac_and_both_filled():
    iface = MagicMock()
    iface.mac_address = "44:4C:A8:BF:2E:91"
    iface.mac_addresses = [{"mac_address": "44:4C:A8:BF:2E:91"}]
    iface.primary_mac_address = 1
    assert _get_interface_mac(iface) == "44:4C:A8:BF:2E:91"
    assert _mac_both_filled(iface) is True


def test_apply_mac_skips_create_when_existing_only(capsys):
    nb = MagicMock()
    nb_iface = MagicMock(id=42)
    nb.dcim.mac_addresses.filter.return_value = []

    _apply_mac_to_interface(nb, "dev1", "Eth1", nb_iface, "44:4c:a8:bf:2e:91", existing_only=True)

    nb.dcim.mac_addresses.create.assert_not_called()
    assert "skipped (--existing-only)" in capsys.readouterr().out


def test_apply_ip_skips_create_when_existing_only(capsys):
    nb = MagicMock()
    nb_iface = MagicMock(id=10)
    with patch("netbox_checks._get_interface_ip_addresses", return_value=[]):
        with patch("netbox_checks._find_ip_in_netbox", return_value=[]):
            with patch("netbox_checks._find_ip_in_netbox_any_vrf", return_value=[]):
                _apply_ip_addresses_to_interface(
                    nb, "R1", "Eth1", nb_iface, ["203.0.113.2/24"], vrf_id_f=None, existing_only=True
                )
    nb.ipam.ip_addresses.create.assert_not_called()
    assert "skipped (--existing-only)" in capsys.readouterr().out


def test_apply_ip_skips_unbind_when_existing_only(capsys):
    nb = MagicMock()
    nb_iface = MagicMock(id=10)
    existing_ip = MagicMock()
    existing_ip.assigned_object_id = 10
    existing_ip.assigned_object_type = "dcim.interface"
    existing_ip.save = MagicMock()
    with patch("netbox_checks._get_interface_ip_addresses", return_value=[("203.0.113.9/24", None)]):
        with patch("netbox_checks._find_ip_in_netbox", return_value=[existing_ip]):
            _apply_ip_addresses_to_interface(nb, "R1", "Eth1", nb_iface, [], vrf_id_f=None, existing_only=True)
    existing_ip.save.assert_not_called()
    assert existing_ip.assigned_object_id == 10
    assert "extra on interface, skipped (--existing-only)" in capsys.readouterr().out


def test_apply_ip_skips_rebind_when_existing_only(capsys):
    nb = MagicMock()
    nb_iface = MagicMock(id=10)
    existing_ip = MagicMock()
    existing_ip.assigned_object_id = 99
    existing_ip.assigned_object_type = "dcim.interface"
    existing_ip.save = MagicMock()
    with patch("netbox_checks._get_interface_ip_addresses", return_value=[]):
        with patch("netbox_checks._find_ip_in_netbox", return_value=[existing_ip]):
            _apply_ip_addresses_to_interface(
                nb, "R1", "Eth1", nb_iface, ["203.0.113.2/24"], vrf_id_f=None, existing_only=True
            )
    existing_ip.save.assert_not_called()
    assert existing_ip.assigned_object_id == 99
    assert "not bound to this interface, skipped (--existing-only)" in capsys.readouterr().out


def test_apply_mac_skips_rebind_when_existing_only(capsys):
    nb = MagicMock()
    nb_iface = MagicMock(id=20)
    nb_iface.update = MagicMock()
    old_iface = MagicMock(id=5, primary_mac_address=99)
    old_iface.update = MagicMock()
    mac_rec = MagicMock(id=99, assigned_object_id=5)
    mac_rec.save = MagicMock()
    nb.dcim.mac_addresses.filter.return_value = [mac_rec]
    nb.dcim.interfaces.get.side_effect = lambda pk: old_iface if pk == 5 else nb_iface

    _apply_mac_to_interface(nb, "R1", "Eth1", nb_iface, "44:4C:A8:BF:2E:91", existing_only=True)

    old_iface.update.assert_not_called()
    mac_rec.save.assert_not_called()
    nb_iface.update.assert_not_called()
    assert mac_rec.assigned_object_id == 5
    assert "bound to another interface, skipped (--existing-only)" in capsys.readouterr().out


def test_apply_mac_skips_unassigned_bind_when_existing_only(capsys):
    nb = MagicMock()
    nb_iface = MagicMock(id=20)
    nb_iface.update = MagicMock()
    mac_rec = MagicMock(id=99, assigned_object_id=None)
    mac_rec.save = MagicMock()
    nb.dcim.mac_addresses.filter.return_value = [mac_rec]

    _apply_mac_to_interface(nb, "R1", "Eth1", nb_iface, "44:4C:A8:BF:2E:91", existing_only=True)

    mac_rec.save.assert_not_called()
    nb_iface.update.assert_not_called()
    assert "not bound to this interface, skipped (--existing-only)" in capsys.readouterr().out


def test_apply_mac_creates_and_sets_primary():
    nb = MagicMock()
    nb_iface = MagicMock()
    nb_iface.id = 42
    nb_iface.primary_mac_address = None

    mac_rec = MagicMock()
    mac_rec.id = 7
    mac_rec.url = "http://netbox/mac/7"
    mac_rec.assigned_object_id = None
    mac_rec.save = MagicMock()

    nb.dcim.mac_addresses.filter.return_value = []
    nb.dcim.mac_addresses.create.return_value = mac_rec

    _apply_mac_to_interface(nb, "dev1", "Eth1", nb_iface, "44:4c:a8:bf:2e:91")

    nb.dcim.mac_addresses.create.assert_called_once()
    nb_iface.update.assert_called_with({"primary_mac_address": 7})


def test_apply_mac_existing_on_same_interface():
    nb = MagicMock()
    nb_iface = MagicMock()
    nb_iface.id = 42

    mac_rec = MagicMock()
    mac_rec.id = 7
    mac_rec.url = "http://netbox/mac/7"
    mac_rec.assigned_object_id = 42

    nb.dcim.mac_addresses.filter.return_value = [mac_rec]

    _apply_mac_to_interface(nb, "dev1", "Eth1", nb_iface, "44:4c:a8:bf:2e:91")
    nb.dcim.mac_addresses.create.assert_not_called()
