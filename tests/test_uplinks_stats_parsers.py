"""uplinks_stats.py pure helpers (no SSH)."""

import json

from uplinks_stats import (
    _arista_interface_link_up,
    _format_ssh_connect_error,
    _is_global_routable_address,
    _juniper_iface_name_desc,
    _juniper_speed_to_bps,
    _parse_arista_interface_ips,
    arista_cli_interface_name,
    extract_json,
    is_arista_platform,
    is_juniper_platform,
)


def test_extract_json_from_noise():
    text = 'noise before {"a": 1, "b": [2]} trailing'
    assert extract_json(text) == {"a": 1, "b": [2]}


def test_juniper_speed_to_bps():
    assert _juniper_speed_to_bps("10gbps") == 10_000_000_000
    assert _juniper_speed_to_bps("1000mbps") == 1_000_000_000
    assert _juniper_speed_to_bps("") is None


def test_juniper_iface_name_desc():
    iface = {"name": [{"data": "ae5.0"}], "description": [{"data": "Uplink: ISP"}]}
    assert _juniper_iface_name_desc(iface) == ("ae5.0", "Uplink: ISP")


def test_is_global_routable_address():
    assert _is_global_routable_address("203.0.113.5/24") is True
    assert _is_global_routable_address("10.0.0.1/8") is False
    assert _is_global_routable_address("fe80::1/64") is False


def test_parse_arista_interface_ips():
    if_obj = {
        "forwardingModel": "routed",
        "interfaceAddress": [
            {"primaryIp": {"address": "203.0.113.1", "maskLen": 24}},
        ],
        "interfaceAddressIp6": {
            "globalUnicastIp6s": [{"address": "2001:db8::1", "subnet": "2001:db8::/64"}],
        },
    }
    ips = _parse_arista_interface_ips(if_obj)
    assert "203.0.113.1/24" in ips["ipv4_addresses"]
    assert any("2001:db8" in a for a in ips["ipv6_addresses"])


def test_arista_interface_link_up():
    assert _arista_interface_link_up({"lineProtocolStatus": "up", "interfaceStatus": "connected"}) is True
    assert _arista_interface_link_up({"lineProtocolStatus": "down"}) is False


def test_platform_detection():
    assert is_juniper_platform("Juniper JunOS") is True
    assert is_arista_platform("Arista EOS") is True
    assert is_juniper_platform("Arista") is False


def test_arista_cli_interface_name():
    assert arista_cli_interface_name("Ethernet51/1") == "ethernet 51/1"


def test_format_ssh_connect_error():
    err = _format_ssh_connect_error("host1", OSError(51, "Network unreachable"))
    assert "host1" in err
    assert "51" in err or "Network" in err
