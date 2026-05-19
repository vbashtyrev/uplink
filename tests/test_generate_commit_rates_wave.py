"""generate_commit_rates additional branches."""

import json
from pathlib import Path

from generate_commit_rates import build_circuit_id_map, is_uplink, load_json, location_from_hostname


def test_is_uplink_and_location():
    assert is_uplink({"description": "Uplink: ISP"}) is True
    assert is_uplink({"description": "mgmt"}) is False
    assert location_from_hostname("ALA-KZT-1") == "ALA"


def test_build_circuit_id_map():
    dry = {
        "devices": {
            "R1": [
                {"name": "Eth1", "description": "Uplink: Cogent", "bandwidth": 10_000_000_000},
            ],
        },
    }
    desc_map = {"Uplink: Cogent": "Cogent"}
    existing = {"R1": {"Eth1": {"circuit_id": "KEEP-1"}}}
    cid_map = build_circuit_id_map(dry, desc_map, existing)
    assert cid_map[("R1", "Eth1")] == "KEEP-1"


def test_load_json_errors(tmp_path):
    assert load_json(str(tmp_path / "nope.json"), default={}) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    result = load_json(str(bad))
    assert isinstance(result, tuple)
