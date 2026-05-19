"""Additional uplinks_stats parser and helper coverage."""

import xml.etree.ElementTree as ET

from uplinks_stats import (
    _extract_all_xml_interface_information_blocks,
    _extract_xml_interface_information,
    _juniper_data,
    _juniper_speed_to_bps,
    _juniper_uplink_is_unit0,
    _parse_junos_rpc_reply_and_find_interface_information,
    extract_json,
    parse_juniper_descriptions_all,
    parse_juniper_uplinks,
    parse_juniper_uplinks_from_xml,
)

XML_BLOCK = """<interface-information>
<logical-interface><name>ae5.0</name><description>Uplink: X</description><oper-status>up</oper-status></logical-interface>
</interface-information>"""


def test_juniper_speed_variants():
    assert _juniper_speed_to_bps("100kbps") == 100_000
    assert _juniper_speed_to_bps("1000bps") == 1000
    assert _juniper_speed_to_bps("bad") is None


def test_juniper_uplink_is_unit0():
    assert _juniper_uplink_is_unit0("ae5") is True
    assert _juniper_uplink_is_unit0("ae5.100") is False


def test_juniper_data_scalar():
    assert _juniper_data([{"data": "up"}]) == "up"
    assert _juniper_data([]) is None


def test_parse_juniper_skips_vlan_unit():
    data = {
        "interface-information": [{
            "logical-interface": [{
                "name": [{"data": "ae5.100"}],
                "description": [{"data": "Uplink: VLAN"}],
                "oper-status": [{"data": "up"}],
            }],
        }],
    }
    assert parse_juniper_uplinks(data) == []


def test_parse_juniper_descriptions_all():
    data = {
        "interface-information": [{
            "physical-interface": [{
                "name": [{"data": "et-0/0/1"}],
                "description": [{"data": "Uplink: ISP"}],
                "oper-status": [{"data": "up"}],
            }],
        }],
    }
    rows = parse_juniper_descriptions_all(data)
    assert rows[0][0] == "et-0/0/1"


def test_extract_json_invalid():
    assert extract_json("{bad json") is None
    assert extract_json("no json") is None


def test_xml_extraction_and_parse():
    blocks = _extract_all_xml_interface_information_blocks(XML_BLOCK)
    assert blocks
    root = ET.fromstring(blocks[0])
    uplinks = parse_juniper_uplinks_from_xml(root, require_link_up=True)
    assert any(n == "ae5.0" for n, _ in uplinks)
    assert _extract_xml_interface_information(XML_BLOCK) is not None


def test_rpc_reply_parse():
    text = "<rpc-reply>" + XML_BLOCK + "</rpc-reply>"
    roots = _parse_junos_rpc_reply_and_find_interface_information(text)
    assert roots
