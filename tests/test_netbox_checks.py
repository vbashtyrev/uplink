"""Tests for netbox_checks.py helpers."""

from netbox_checks import (
    check_intname,
    compare_hostnames,
    interface_name_variants,
)


def test_interface_name_variants_ethernet():
    variants = interface_name_variants("Ethernet51/1")
    assert variants[0] == "Ethernet51/1"
    assert "ethernet51/1" in variants
    assert "Eth51/1" in variants
    assert "Ethernet51" in variants


def test_check_intname_exact_and_alternate():
    nb = {"Ethernet51/1": object(), "eth51/1": object()}
    status, nb_name, note = check_intname("dev", "Ethernet51/1", nb)
    assert status == "ok"
    assert nb_name == "Ethernet51/1"
    assert note == ""

    status, nb_name, note = check_intname("dev", "Ethernet51/1", {"eth51/1": object()})
    assert status == "found"
    assert nb_name == "eth51/1"


def test_compare_hostnames():
    only_file, only_nb = compare_hostnames(["a", "b"], ["b", "c"])
    assert only_file == ["a"]
    assert only_nb == ["c"]
