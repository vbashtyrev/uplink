"""Juniper-specific uplinks_stats helpers."""

import json
from pathlib import Path

from uplinks_stats import (
    _juniper_ae_bundle_name,
    _juniper_interface_slot,
    _juniper_lacp_member_names,
    _juniper_optics_tx_power_dbm,
    _parse_juniper_phy_iface,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_juniper_lacp_member_names():
    lacp = json.loads((FIXTURES / "juniper_ssh_lacp.json").read_text(encoding="utf-8"))
    names = _juniper_lacp_member_names(lacp)
    assert "et-0/0/1" in names


def test_juniper_interface_slot():
    assert _juniper_interface_slot("et-0/0/3") == (0, 0, 3)
    assert _juniper_interface_slot("bad") is None


def test_juniper_ae_bundle_name():
    ae5 = json.loads((FIXTURES / "juniper_ssh_ae5.json").read_text(encoding="utf-8"))
    bundle = _juniper_ae_bundle_name(ae5)
    assert bundle is None or isinstance(bundle, str)


def test_juniper_optics_and_phy_iface():
    optics = json.loads((FIXTURES / "juniper_ssh_optics.json").read_text(encoding="utf-8"))
    tx = _juniper_optics_tx_power_dbm(optics)
    assert tx is None or isinstance(tx, (int, float))
    ae5 = json.loads((FIXTURES / "juniper_ssh_ae5.json").read_text(encoding="utf-8"))
    infos = ae5.get("interface-information") or []
    phys = infos[0].get("physical-interface") or []
    if phys:
        stats = _parse_juniper_phy_iface(phys[0] if isinstance(phys, list) else phys)
        assert isinstance(stats, dict)
