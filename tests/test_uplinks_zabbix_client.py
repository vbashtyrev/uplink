"""Tests for uplinks.zabbix.client."""

from uplinks.zabbix.client import (
    interface_from_item_name,
    interface_from_key,
    normalize_interface_name,
)


def test_interface_parsers():
    assert interface_from_key('net.if.in["Ethernet51/1"]') == "Ethernet51/1"
    assert interface_from_item_name("Interface Eth1(Uplink): Bits received") == "Eth1"
    assert normalize_interface_name("Eth1") == "eth1"
