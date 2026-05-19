"""interface_name_variants and compare_hostnames."""

import netbox_checks as nc


def test_interface_name_variants_ethernet_eth_et():
    v = nc.interface_name_variants("Ethernet51/1")
    assert "ethernet51/1" in v
    assert "Eth51/1" in v
    assert "Ethernet51" in v
    v2 = nc.interface_name_variants("Eth49/2")
    assert "eth49/2" in v2
    v3 = nc.interface_name_variants("Et10/1")
    assert "et10/1" in v3


def test_interface_name_variants_empty_and_other():
    assert nc.interface_name_variants("") == []
    assert "ae5.0" in nc.interface_name_variants("ae5.0")


def test_compare_hostnames():
    only_f, only_n = nc.compare_hostnames(["A", "B"], ["B", "C"])
    assert only_f == ["A"]
    assert only_n == ["C"]
