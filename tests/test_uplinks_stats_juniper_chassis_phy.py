"""_juniper_chassis_media_type and _parse_juniper_phy_iface."""

import json
from pathlib import Path

from uplinks_stats import _juniper_chassis_media_type, _parse_juniper_phy_iface, _juniper_data

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_juniper_chassis_media_type_from_fixture():
    chassis = {
        "chassis-inventory": [{
            "chassis": [{
                "chassis-module": [{
                    "name": [{"data": "FPC 0"}],
                    "chassis-sub-module": [{
                        "name": [{"data": "PIC 0"}],
                        "chassis-sub-sub-module": [{
                            "name": [{"data": "Xcvr 0"}],
                            "description": [{"data": "SFP-10G-SR"}],
                        }],
                    }],
                }],
            }],
        }],
    }
    mt = _juniper_chassis_media_type(chassis, 0, 0, 0)
    assert mt == "SFP-10G-SR"


def test_parse_juniper_phy_iface_duplex_and_speed():
    ph = {
        "name": [{"data": "et-0/0/1"}],
        "description": [{"data": "Uplink: test"}],
        "speed": [{"data": "10gbps"}],
        "mtu": [{"data": "9192"}],
        "current-physical-address": [{"data": "aa:bb:cc:dd:ee:ff"}],
        "link-type": [{"data": "Full-Duplex"}],
    }
    stats = _parse_juniper_phy_iface(ph)
    assert stats["duplex"] == "full"
    assert stats["bandwidth"] == 10_000_000_000
    assert stats["mtu"] == 9192


def test_parse_juniper_phy_iface_half_duplex():
    ph = {
        "name": [{"data": "ge-0/0/0"}],
        "description": [{"data": ""}],
        "speed": [{"data": "1000mbps"}],
        "link-type": [{"data": "Half-Duplex"}],
    }
    assert _parse_juniper_phy_iface(ph)["duplex"] == "half"


def test_juniper_data_empty():
    assert _juniper_data(None) is None
    assert _juniper_data([{"data": ""}]) is None
