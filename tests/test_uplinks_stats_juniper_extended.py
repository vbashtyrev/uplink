"""Extended Juniper/XML and helper coverage for uplinks_stats."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

from uplinks_stats import (
    _extract_all_xml_interface_information_blocks,
    _extract_xml_interface_information,
    _juniper_ae_bundle_name,
    _juniper_chassis_media_type,
    _juniper_interface_slot,
    _juniper_lacp_member_names,
    _juniper_optics_tx_power_dbm,
    _juniper_speed_to_bps,
    _juniper_xml_child,
    _juniper_xml_elem_text,
    _juniper_xml_iface_name_desc_oper,
    _load_ssh_config,
    _load_stats_file,
    _parse_juniper_logical_ip_addresses,
    _parse_juniper_logical_mtu,
    _parse_juniper_phy_iface,
    _parse_junos_rpc_reply_and_find_interface_information,
    _resolve_ssh_host,
    netbox_error_message,
    parse_juniper_descriptions_all,
    parse_juniper_uplinks_from_xml,
    process_one_juniper,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_juniper_xml_helpers():
    xml = """
    <rpc-reply>
      <interface-information>
        <physical-interface>
          <name>et-0/0/1</name>
          <description>Uplink: ISP</description>
          <oper-status>up</oper-status>
        </physical-interface>
      </interface-information>
    </rpc-reply>
    """
    roots = _parse_junos_rpc_reply_and_find_interface_information(xml)
    assert len(roots) >= 1
    uplinks = parse_juniper_uplinks_from_xml(roots[0], require_link_up=True)
    assert any(n == "et-0/0/1" for n, _ in uplinks)


def test_extract_xml_blocks():
    text = "<interface-information></interface-information><interface-information/>"
    blocks = _extract_all_xml_interface_information_blocks(text)
    assert len(blocks) >= 1
    root = _extract_xml_interface_information(text)
    assert root is not None


def test_juniper_helper_functions():
    assert _juniper_speed_to_bps("10gbps") == 10_000_000_000
    assert _juniper_interface_slot("et-0/0/1") == (0, 0, 1)
    lacp = {
        "lacp-interface-information-list": [{
            "lacp-interface-information": [{
                "lag-lacp-state": [{"name": [{"data": "et-0/0/1"}]}],
            }],
        }],
    }
    assert _juniper_lacp_member_names(lacp) == ["et-0/0/1"]
    diag = {
        "interface-information": [{
            "physical-interface": [{
                "optics-diagnostics": [{
                    "optics-diagnostics-lane-values": [{
                        "laser-output-power-dbm": [{"data": "-2.5"}],
                    }],
                }],
            }],
        }],
    }
    assert _juniper_optics_tx_power_dbm(diag) == -2.5
    chassis = {"chassis-inventory": {"chassis": [{"name": [{"data": "Chassis"}],
        "chassis-module": [{"name": [{"data": "FPC 0"}], "pic": [{"name": [{"data": "PIC 0"}],
        "port": [{"name": [{"data": "Port 1"}], "sfp-type": [{"data": "10G-SR"}]}]}]}]}]}}
    mt = _juniper_chassis_media_type(chassis, 0, 0, 1)
    assert mt is None or isinstance(mt, str)


def test_parse_juniper_phy_and_logical():
    ph = {
        "name": [{"data": "et-0/0/1"}],
        "description": [{"data": "Uplink: X"}],
        "speed": [{"data": "10Gbps"}],
        "oper-status": [{"data": "up"}],
        "eth-switch-error": [{"data": "none"}],
    }
    row = _parse_juniper_phy_iface(ph)
    assert row["name"] == "et-0/0/1"
    log_iface = {
        "name": [{"data": "ae5.0"}],
        "address-family": [
            {"address-family-name": [{"data": "inet"}], "mtu": [{"data": "9192"}],
             "interface-address": [{"ifa-local": [{"data": "203.0.113.1/24"}]}]},
        ],
    }
    assert _parse_juniper_logical_mtu(log_iface) == 9192
    ips = _parse_juniper_logical_ip_addresses(log_iface)
    assert ips["ipv4_addresses"]


def test_parse_juniper_descriptions_all():
    data = json.loads((FIXTURES / "juniper_descriptions.json").read_text(encoding="utf-8"))
    all_desc = parse_juniper_descriptions_all(data)
    assert any("Uplink:" in (row[1] or "") for row in all_desc)


def test_load_stats_file_and_netbox_errors(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"devices": {}}), encoding="utf-8")
    data, err = _load_stats_file(str(p))
    assert err is None
    assert "devices" in data
    assert "token" in netbox_error_message(Exception("401 Unauthorized")).lower()


def test_resolve_ssh_host_no_config():
    host, user = _resolve_ssh_host(None, "dev1", "dev1.example", "admin")
    assert host == "dev1.example"
    assert user == "admin"


def test_process_one_juniper(monkeypatch):
    device = MagicMock()
    device.name = "FRN-MX-1"
    monkeypatch.setattr(
        "uplinks_stats.get_juniper_uplink_stats",
        lambda *a, **k: ([{"name": "et-0/0/1"}], None),
    )
    name, payload = process_one_juniper(
        device, None, "u", "p", ".ex", lambda d, m: None
    )
    assert name == "FRN-MX-1"
    assert payload[0]["name"] == "et-0/0/1"
