"""Tests for uplinks_stats.py parsing helpers (no SSH/NetBox)."""

import json
from pathlib import Path

from uplinks_stats import (
    parse_arista_uplinks,
    parse_juniper_uplinks,
    _juniper_uplink_is_unit0,
)
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_juniper_uplink_is_unit0():
    assert _juniper_uplink_is_unit0("et-0/0/1") is True
    assert _juniper_uplink_is_unit0("ae5.0") is True
    assert _juniper_uplink_is_unit0("ae5.100") is False


def test_parse_juniper_uplinks():
    data = json.loads((FIXTURES / "juniper_descriptions.json").read_text(encoding="utf-8"))
    uplinks = parse_juniper_uplinks(data, require_link_up=True)
    names = {n for n, _ in uplinks}
    assert "et-0/0/1" in names
    assert "ae5.0" in names
    assert "ae5.100" not in names
    assert "et-0/0/2" not in names


def test_parse_arista_uplinks():
    data = json.loads((FIXTURES / "arista_descriptions.json").read_text(encoding="utf-8"))
    uplinks = parse_arista_uplinks(data)
    assert uplinks == [("Ethernet51/1", "Uplink: Cogent")]
