"""netbox_checks helper functions and table printing."""

from unittest.mock import MagicMock

import netbox_checks as nc


def test_normalize_duplex_and_fwd():
    assert nc._normalize_duplex("duplexFull") == "full"
    assert nc._normalize_duplex("half") == "half"
    assert nc._normalize_duplex(None) == ""
    assert nc._fwd_file_to_netbox_mode("routed") is None
    assert nc._fwd_file_to_netbox_mode("bridged") == "tagged"
    assert nc._fwd_file_to_netbox_mode("custom") == "custom"


def test_mac_helpers():
    assert nc._normalize_mac("44-4C-A8-BF-2E-91") == "44:4c:a8:bf:2e:91"
    assert nc._mac_netbox_format("44:4c:a8:bf:2e:91") == "44:4C:A8:BF:2E:91"
    iface = MagicMock(mac_address="aa:bb:cc:dd:ee:ff", mac_addresses=[])
    assert nc._get_interface_mac(iface) == "aa:bb:cc:dd:ee:ff"
    iface2 = MagicMock(mac_address=None, mac_addresses=[{"mac_address": "11:22:33:44:55:66"}])
    assert "11:22" in nc._get_interface_mac(iface2)
    both = MagicMock(mac_address="aa:bb:cc:dd:ee:ff", primary_mac_address=None, mac_addresses=[{"mac_address": "aa:bb:cc:dd:ee:ff"}])
    assert nc._mac_both_filled(both) is True


def test_ip_normalization_ipv6_branches():
    assert nc._is_global_routable_address("::1/128") is False
    assert nc._is_global_routable_address("fe80::1/64") is False
    assert nc._is_global_routable_address("fc00::1/64") is False
    assert nc._is_global_routable_address("not-an-ip") is False
    assert nc._normalize_ip_address(" 2001:DB8::1/64 ") == "2001:db8::1/64"


def test_interface_name_variants():
    v = nc.interface_name_variants("Ethernet51/1")
    assert "Ethernet51/1" in v
    assert any("ethernet" in x.lower() for x in v)


def test_mt_ref_helpers():
    values = {"10gbase-x-sfpp"}
    lst = [{"value": "10gbase-x-sfpp", "label": "SFP+"}]
    assert nc._mt_in_ref("10gbase-x-sfpp", values, lst)
    assert nc._mt_to_value("SFP+", values, lst) == "10gbase-x-sfpp"


def test_print_combined_table(capsys):
    rows = [
        ("host1", "eth1", "eth1", "OK", "short", "short", 0, "mt", "mt", 0, "", 0, 0, "", "", 0, "", "", 0, "", "", 0, "", "", 0, "", 0, "", "", 0, "", "", "", "", 0, "", "", 0, "", "", 0),
    ]
    col_spec = [("Host", 0, 20), ("Iface", 1, 15), ("Note", 3, 10)]
    nc._print_combined_table(rows, {nc.NOTE_MISSING}, col_spec)
    out = capsys.readouterr().out
    assert "host1" in out
    assert "not found" in out.lower() or "—" in out
