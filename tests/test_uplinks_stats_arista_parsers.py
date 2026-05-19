"""Arista parser helpers in uplinks_stats."""

from uplinks_stats import _arista_interface_link_up, _is_global_routable_address, parse_arista_uplinks


def test_parse_arista_uplinks_skips_non_dict():
    data = {"interfaceDescriptions": {"Eth1": "not-a-dict", "Eth2": {"description": "Uplink: ISP"}}}
    out = parse_arista_uplinks(data)
    assert out == [("Eth2", "Uplink: ISP")]


def test_arista_interface_link_up_states():
    assert _arista_interface_link_up({"lineProtocolStatus": "down"}) is False
    assert _arista_interface_link_up({"interfaceStatus": "notconnect"}) is False
    assert _arista_interface_link_up({"lineProtocolStatus": "up", "interfaceStatus": "connected"}) is True


def test_is_global_routable_ipv6_edge():
    assert _is_global_routable_address("fe80::1/64") is False
    assert _is_global_routable_address("2001:db8::1/64") is True
