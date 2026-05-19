"""netbox_create_circuits tag automation and loaders."""

import json
from unittest.mock import MagicMock

import netbox_create_circuits as ncc
from tests.mocks.netbox_full import NetBoxTestEnvironment


def test_load_commit_rates_errors(tmp_path):
    missing, err = ncc.load_commit_rates(str(tmp_path / "nope.json"))
    assert missing is None
    assert "not found" in err
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    data, err2 = ncc.load_commit_rates(str(bad))
    assert data is None
    assert "JSON" in err2


def test_load_dry_ssh_missing():
    assert ncc.load_dry_ssh("/nonexistent/dry.json") is None


def test_get_or_create_automation_tag(monkeypatch):
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    tag = ncc._get_or_create_automation_tag(env)
    assert tag is not None


def test_ensure_record_tag_patches(monkeypatch):
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    tag = ncc._get_or_create_automation_tag(env)
    prov, _ = env._ensure_provider("Cogent")
    ncc._ensure_record_tag(env, prov, tag, env.circuits.providers)
    full = env.circuits.providers.get(prov.id)
    assert full is not None


def test_resolve_physical_interface():
    devices = {
        "R1": [{"name": "ae5.0", "isLogical": True, "physicalInterface": "et-0/0/1"}],
    }
    assert ncc.resolve_physical_interface("R1", "ae5.0", devices) == "et-0/0/1"
    assert ncc.resolve_physical_interface("R1", "Eth1", devices) == "Eth1"


def test_location_from_hostname():
    assert ncc.location_from_hostname("ALA-KZT-7280TR-1") == "ALA"
