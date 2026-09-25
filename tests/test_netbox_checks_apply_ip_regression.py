"""Regression tests for netbox_checks IP apply fail-closed behavior."""

from unittest.mock import MagicMock, patch

import netbox_checks as nc


def _nb_iface(iface_id=10):
    iface = MagicMock()
    iface.id = iface_id
    return iface


def test_apply_ip_read_error_skips_mutations(capsys):
    nb = MagicMock()
    nb_iface = _nb_iface()
    with patch.object(nc, "_get_interface_ip_addresses", return_value=(None, "timeout")):
        nc._apply_ip_addresses_to_interface(
            nb, "R1", "Eth1", nb_iface, ["203.0.113.2/24"], vrf_id_f=None
        )
    nb.ipam.ip_addresses.create.assert_not_called()
    assert "timeout" in capsys.readouterr().err


def test_apply_ip_ambiguous_candidates_skips_create(capsys):
    nb = MagicMock()
    nb_iface = _nb_iface()
    dup1 = MagicMock(assigned_object_id=None, assigned_object_type=None)
    dup2 = MagicMock(assigned_object_id=None, assigned_object_type=None)
    with patch.object(nc, "_get_interface_ip_addresses", return_value=([], None)):
        with patch.object(nc, "_find_ips_in_netbox", return_value=([dup1, dup2], None)):
            nc._apply_ip_addresses_to_interface(
                nb, "R1", "Eth1", nb_iface, ["203.0.113.2/24"], vrf_id_f=None
            )
    nb.ipam.ip_addresses.create.assert_not_called()
    assert "ambiguous" in capsys.readouterr().err


def test_apply_ip_missing_in_vrf_creates(capsys):
    nb = MagicMock()
    nb_iface = _nb_iface()
    with patch.object(nc, "_get_interface_ip_addresses", return_value=([], None)):
        with patch.object(nc, "_find_ips_in_netbox", return_value=([], None)):
            nc._apply_ip_addresses_to_interface(
                nb, "R1", "Eth1", nb_iface, ["203.0.113.2/24"], vrf_id_f=7
            )
    nb.ipam.ip_addresses.create.assert_called_once()


def test_find_ips_vrf_filter_mismatch_is_lookup_error():
    nb = MagicMock()
    wrong_vrf_ip = MagicMock()
    wrong_vrf_ip.vrf = MagicMock(id=99)
    nb.ipam.ip_addresses.filter.return_value = [wrong_vrf_ip]
    candidates, err = nc._find_ips_in_netbox(nb, "203.0.113.2/24", 7)
    assert candidates is None
    assert err
    assert "VRF mismatch" in err


def test_apply_ip_filter_returns_wrong_vrf_skips_mutations(capsys):
    nb = MagicMock()
    nb_iface = _nb_iface()
    wrong_vrf_ip = MagicMock(
        address="203.0.113.2/24",
        assigned_object_id=None,
        assigned_object_type=None,
        vrf=MagicMock(id=99),
    )
    wrong_vrf_ip.save = MagicMock()
    nb.ipam.ip_addresses.filter.return_value = [wrong_vrf_ip]
    with patch.object(nc, "_get_interface_ip_addresses", return_value=([], None)):
        nc._apply_ip_addresses_to_interface(
            nb, "R1", "Eth1", nb_iface, ["203.0.113.2/24"], vrf_id_f=7
        )
    nb.ipam.ip_addresses.create.assert_not_called()
    wrong_vrf_ip.save.assert_not_called()
    assert "VRF mismatch" in capsys.readouterr().err


def test_apply_ip_existing_only_skips_create(capsys):
    nb = MagicMock()
    nb_iface = _nb_iface()
    with patch.object(nc, "_get_interface_ip_addresses", return_value=([], None)):
        with patch.object(nc, "_find_ips_in_netbox", return_value=([], None)):
            nc._apply_ip_addresses_to_interface(
                nb,
                "R1",
                "Eth1",
                nb_iface,
                ["203.0.113.2/24"],
                vrf_id_f=None,
                existing_only=True,
            )
    nb.ipam.ip_addresses.create.assert_not_called()
    assert "skipped (--existing-only)" in capsys.readouterr().out


def test_apply_ip_existing_only_skips_unbind(capsys):
    nb = MagicMock()
    nb_iface = _nb_iface()
    ip_obj = MagicMock(assigned_object_id=10, assigned_object_type="dcim.interface")
    ip_obj.save = MagicMock()
    with patch.object(
        nc,
        "_get_interface_ip_addresses",
        return_value=([("203.0.113.9/24", None)], None),
    ):
        with patch.object(nc, "_find_ips_in_netbox", return_value=([ip_obj], None)):
            with patch.object(nc, "_ip_assigned_to_interface", return_value=True):
                nc._apply_ip_addresses_to_interface(
                    nb, "R1", "Eth1", nb_iface, [], vrf_id_f=None, existing_only=True
                )
    ip_obj.save.assert_not_called()


def test_apply_ip_existing_only_skips_rebind(capsys):
    nb = MagicMock()
    nb_iface = _nb_iface()
    ip_obj = MagicMock(assigned_object_id=99, assigned_object_type="dcim.interface")
    ip_obj.save = MagicMock()
    with patch.object(nc, "_get_interface_ip_addresses", return_value=([], None)):
        with patch.object(nc, "_find_ips_in_netbox", return_value=([ip_obj], None)):
            with patch.object(nc, "_ip_assigned_to_interface", return_value=False):
                nc._apply_ip_addresses_to_interface(
                    nb,
                    "R1",
                    "Eth1",
                    nb_iface,
                    ["203.0.113.2/24"],
                    vrf_id_f=None,
                    existing_only=True,
                )
    ip_obj.save.assert_not_called()


def test_apply_ip_successful_bind(capsys):
    nb = MagicMock()
    nb_iface = _nb_iface()
    ip_obj = MagicMock(address="203.0.113.2/24", assigned_object_id=None, assigned_object_type=None)
    ip_obj.save = MagicMock()
    with patch.object(nc, "_get_interface_ip_addresses", return_value=([], None)):
        with patch.object(nc, "_find_ips_in_netbox", return_value=([ip_obj], None)):
            with patch.object(nc, "_ip_assigned_to_interface", return_value=False):
                nc._apply_ip_addresses_to_interface(
                    nb, "R1", "Eth1", nb_iface, ["203.0.113.2/24"], vrf_id_f=None
                )
    assert ip_obj.assigned_object_id == 10
    assert ip_obj.assigned_object_type == "dcim.interface"
    ip_obj.save.assert_called_once()


def test_resolve_vrf_does_not_cache_read_error():
    nb = MagicMock()
    nb.ipam.vrfs.filter.side_effect = RuntimeError("netbox down")
    cache = {}
    error_cache = {}
    vrf_id, err = nc._resolve_vrf_name_to_id(nb, "internet", cache, error_cache)
    assert vrf_id is None
    assert err
    assert "internet" in error_cache
    assert "internet" not in cache


def test_resolve_vrf_missing_name_returns_error():
    nb = MagicMock()
    nb.ipam.vrfs.filter.return_value = []
    cache = {}
    error_cache = {}
    vrf_id, err = nc._resolve_vrf_name_to_id(nb, "missing-vrf", cache, error_cache)
    assert vrf_id is None
    assert "not found" in err
    assert "missing-vrf" in error_cache
    assert "missing-vrf" not in cache


def test_ip_assigned_to_interface_requires_dcim_type():
    nb_iface = _nb_iface()
    wrong_type = MagicMock(
        assigned_object_id=10, assigned_object_type="virtualization.vminterface"
    )
    empty_type = MagicMock(assigned_object_id=10, assigned_object_type=None)
    correct = MagicMock(assigned_object_id=10, assigned_object_type="dcim.interface")
    assert nc._ip_assigned_to_interface(wrong_type, nb_iface) is False
    assert nc._ip_assigned_to_interface(empty_type, nb_iface) is False
    assert nc._ip_assigned_to_interface(correct, nb_iface) is True


def test_apply_ip_missing_assigned_object_type_skips_bind(capsys):
    nb = MagicMock()
    nb_iface = _nb_iface()
    ip_obj = MagicMock(
        address="203.0.113.2/24",
        assigned_object_id=10,
        assigned_object_type=None,
    )
    ip_obj.save = MagicMock()
    with patch.object(nc, "_get_interface_ip_addresses", return_value=([], None)):
        with patch.object(nc, "_find_ips_in_netbox", return_value=([ip_obj], None)):
            nc._apply_ip_addresses_to_interface(
                nb, "R1", "Eth1", nb_iface, ["203.0.113.2/24"], vrf_id_f=None
            )
    ip_obj.save.assert_not_called()
    nb.ipam.ip_addresses.create.assert_not_called()
    assert "assigned_object_type missing" in capsys.readouterr().err


def test_apply_ip_wrong_assigned_object_type_not_bound(capsys):
    nb = MagicMock()
    nb_iface = _nb_iface()
    ip_obj = MagicMock(
        assigned_object_id=99,
        assigned_object_type="virtualization.vminterface",
    )
    with patch.object(nc, "_get_interface_ip_addresses", return_value=([], None)):
        with patch.object(nc, "_find_ips_in_netbox", return_value=([ip_obj], None)):
            nc._apply_ip_addresses_to_interface(
                nb, "R1", "Eth1", nb_iface, ["203.0.113.2/24"], vrf_id_f=None
            )
    ip_obj.save.assert_not_called()
    nb.ipam.ip_addresses.create.assert_not_called()
    assert "assigned_object_type" in capsys.readouterr().err
