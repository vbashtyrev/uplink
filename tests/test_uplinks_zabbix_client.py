"""Tests for uplinks.zabbix.client."""

from uplinks.zabbix.client import (
    interface_from_item_name,
    interface_from_key,
    is_destructive_call,
    normalize_interface_name,
)


def test_interface_parsers():
    assert interface_from_key('net.if.in["Ethernet51/1"]') == "Ethernet51/1"
    assert interface_from_item_name("Interface Eth1(Uplink): Bits received") == "Eth1"
    assert normalize_interface_name("Eth1") == "eth1"


def test_is_destructive_call():
    assert is_destructive_call("host.delete", {})
    assert is_destructive_call("usermacro.delete", {"hostmacroids": ["1"]})
    assert is_destructive_call("dashboard.create", {})
    assert is_destructive_call("dashboard.update", {"dashboardid": "1"})
    assert is_destructive_call("map.update", {"sysmapid": "1", "links": []})
    assert is_destructive_call("map.update", {"sysmapid": "1", "selements": []})
    assert not is_destructive_call("map.update", {"sysmapid": "1", "width": 100})
    assert is_destructive_call("item.update", {"itemid": "1", "params": "last(/x/y)"})
    assert not is_destructive_call("item.update", {"itemid": "1", "name": "x"})
    assert is_destructive_call("host.update", {"hostid": "1", "macros": []})
    assert not is_destructive_call("host.update", {"hostid": "1", "name": "x"})
    assert is_destructive_call("service.update", {"serviceid": "1", "children": []})
    assert is_destructive_call("service.update", {"serviceid": "1", "problem_tags": []})
    assert not is_destructive_call("service.update", {"serviceid": "1", "name": "x"})
    assert not is_destructive_call("trigger.create", {})
