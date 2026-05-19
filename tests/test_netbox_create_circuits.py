"""Tests for netbox_create_circuits.py helpers and get_or_create_* with mock NetBox."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from netbox_create_circuits import (
    get_or_create_circuit,
    get_or_create_circuit_type,
    get_or_create_provider,
    load_commit_rates,
    load_dry_ssh,
    location_from_hostname,
    resolve_physical_interface,
)
from tests.mocks.netbox_records import MutableNetBox

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_location_from_hostname():
    assert location_from_hostname("ALA-KZT-7280TR-1") == "ALA"
    assert location_from_hostname("") == ""


def test_resolve_physical_interface():
    devices = {
        "mx1": [
            {"name": "ae5.0", "isLogical": True, "physicalInterface": "ae5"},
            {"name": "ae5", "isLag": True},
        ],
    }
    assert resolve_physical_interface("mx1", "ae5.0", devices) == "ae5"
    assert resolve_physical_interface("mx1", "et-0/0/1", devices) == "et-0/0/1"
    assert resolve_physical_interface("unknown", "ae5.0", devices) == "ae5.0"


def test_load_commit_rates_strips_meta(tmp_path):
    path = tmp_path / "cr.json"
    path.write_text(json.dumps({"_provider_limits": {"P": 1}, "host1": {"Eth1": {}}}), encoding="utf-8")
    data, err = load_commit_rates(str(path))
    assert err is None
    assert "_provider_limits" not in data
    assert "host1" in data


def test_load_commit_rates_missing():
    data, err = load_commit_rates("/nonexistent/commit_rates.json")
    assert data is None
    assert "not found" in err


def test_load_dry_ssh():
    path = FIXTURES / "dry_ssh_minimal.json"
    devices = load_dry_ssh(str(path))
    assert "ALA-KZT-7280TR-1" in devices


def test_get_or_create_provider_creates():
    nb = MutableNetBox()
    nb.seed_automation_tag()
    prov, status = get_or_create_provider(nb, "Cogent")
    assert prov is not None
    assert status == "created"
    assert len(nb.circuits.providers._items) == 1


def test_get_or_create_provider_existing():
    nb = MutableNetBox()
    nb.seed_automation_tag()
    existing = nb.circuits.providers.create(name="Cogent", slug="cogent")
    prov, status = get_or_create_provider(nb, "Cogent")
    assert prov.id == existing.id
    assert status is None


def test_get_or_create_circuit_type():
    nb = MutableNetBox()
    nb.seed_automation_tag()
    ct, status = get_or_create_circuit_type(nb, "Internet")
    assert ct is not None
    assert status == "created"


def test_get_or_create_circuit_create_and_update_commit(monkeypatch):
    nb = MutableNetBox()
    nb.seed_automation_tag()
    provider, _ = get_or_create_provider(nb, "Cogent")
    ct, _ = get_or_create_circuit_type(nb, "Internet")

    monkeypatch.setattr(
        "netbox_create_circuits.requests.patch",
        lambda *a, **k: MagicMock(status_code=200, raise_for_status=lambda: None),
    )

    c, status = get_or_create_circuit(nb, "Cogent-ALA-1", provider, ct, commit_rate_kbps=10000000)
    assert status == "created"
    assert c.commit_rate == 10000000

    c2, status2 = get_or_create_circuit(nb, "Cogent-ALA-1", provider, ct, commit_rate_kbps=20000000)
    assert status2 == "commit_rate updated"


def test_patch_circuit_commit_rate_requests(monkeypatch):
    nb = MutableNetBox()
    nb.seed_automation_tag()
    provider, _ = get_or_create_provider(nb, "P")
    ct, _ = get_or_create_circuit_type(nb, "Internet")
    circuit, _ = get_or_create_circuit(nb, "P-1", provider, ct, commit_rate_kbps=5000)

    calls = []

    def fake_patch(url, json=None, headers=None, timeout=30):
        calls.append((url, json))
        return MagicMock(status_code=200, raise_for_status=lambda: None)

    monkeypatch.setattr("netbox_create_circuits.requests.patch", fake_patch)
    from netbox_create_circuits import _patch_circuit_commit_rate

    _patch_circuit_commit_rate(nb, circuit.id, 8000)
    assert calls
    assert "commit_rate" in calls[0][1]
