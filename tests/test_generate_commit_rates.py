"""Tests for generate_commit_rates.py pure helpers."""

import json
from pathlib import Path

from generate_commit_rates import (
    build_circuit_id_map,
    is_uplink,
    load_json,
    location_from_hostname,
)
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_is_uplink():
    assert is_uplink({"description": "Uplink: Cogent"}) is True
    assert is_uplink({"description": "mgmt"}) is False
    assert is_uplink({}) is False


def test_location_from_hostname():
    assert location_from_hostname("ALA-KZT-7280TR-1") == "ALA"
    assert location_from_hostname("") == "other"
    assert location_from_hostname("single") == "single"


def test_build_circuit_id_map_numbering_and_preserve():
    data = json.loads((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"))
    desc_map = {"Uplink: Cogent 10G": "Cogent", "Uplink: Hurricane": "Hurricane", "Uplink: Hurricane member": "Hurricane"}
    existing = {
        "ALA-KZT-7280TR-1": {
            "Ethernet51/1": {"circuit_id": "custom-cid-1", "provider": "Cogent"},
        },
    }
    cid_map = build_circuit_id_map(data, desc_map, existing)

    assert cid_map[("ALA-KZT-7280TR-1", "Ethernet51/1")] == "custom-cid-1"
    assert cid_map[("FRN-MX-1", "ae5.0")] == "Hurricane-FRN-1"
    assert cid_map[("FRN-MX-1", "et-0/0/1")] == "Hurricane-FRN-2"
    assert ("ALA-KZT-7280TR-1", "Ethernet52/1") not in cid_map


def test_load_json_missing_and_invalid(tmp_path):
    assert load_json(str(tmp_path / "missing.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = load_json(str(bad))
    assert isinstance(result, tuple)
    assert result[0] is None
