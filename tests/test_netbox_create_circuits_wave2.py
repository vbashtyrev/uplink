"""netbox_create_circuits: get_or_create, patch helpers, termination edge cases."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from netbox_create_circuits import (
    _patch_circuit_commit_rate,
    _patch_interface_mark_connected,
    create_termination_and_cable,
    get_or_create_circuit,
)
from tests.mocks.netbox_full import NetBoxTestEnvironment


def test_get_or_create_circuit_updates_commit(monkeypatch):
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    prov, _ = env._ensure_provider("Cogent")
    ct = env.circuits.circuit_types._items[0]
    circuit = env.circuits.circuits.create(
        cid="EXIST-1", provider=prov.id, type=ct.id, commit_rate=1000
    )
    monkeypatch.setattr(
        "netbox_create_circuits._patch_circuit_commit_rate",
        lambda nb, cid, rate: setattr(circuit, "commit_rate", rate),
    )
    c, msg = get_or_create_circuit(env, "EXIST-1", prov, ct, commit_rate_kbps=20_000_000)
    assert c is not None
    assert "updated" in (msg or "")


def test_get_or_create_circuit_creates_new():
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    prov, _ = env._ensure_provider("Cogent")
    ct = env.circuits.circuit_types._items[0]
    with patch("netbox_create_circuits._patch_circuit_commit_rate", lambda *a, **k: None):
        c, msg = get_or_create_circuit(env, "NEW-99", prov, ct, commit_rate_kbps=10_000_000)
    assert c is not None
    assert msg == "created"


def test_patch_circuit_commit_rate(monkeypatch):
    nb = MagicMock()
    nb.base_url = "https://nb.example/api"
    nb.token = "tok"
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    calls = []
    monkeypatch.setattr(requests, "patch", lambda *a, **k: calls.append(k) or resp)
    _patch_circuit_commit_rate(nb, 5, 10_000)
    assert calls


def test_create_termination_existing_cable_on_ct():
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    dev = env.dcim.devices._items[0]
    dev.site = 1
    iface = env.add_interface(dev, "Ethernet51/1")
    prov, _ = env._ensure_provider("Cogent")
    ct_type = env.circuits.circuit_types._items[0]
    circuit = env.circuits.circuits.create(
        cid="CKT-X", provider=prov.id, type=ct_type.id, commit_rate=10_000_000
    )
    term = env.circuits.circuit_terminations.create(
        circuit=circuit.id, term_side="A", termination_type="dcim.site", termination_id=1
    )
    term.cable = MagicMock(id=99)
    report = {"deleted_cables": [], "disabled_mark_connected": [], "created_cables": []}
    with patch("netbox_create_circuits._patch_interface_mark_connected", lambda *a, **k: None):
        out, err = create_termination_and_cable(env, circuit, dev, iface, report=report)
    assert err is None
    assert out is not None


def test_create_termination_no_site():
    env = NetBoxTestEnvironment()
    dev = env.add_device("NODE-1")
    iface = env.add_interface(dev, "eth0")
    circuit = MagicMock(id=1)
    out, err = create_termination_and_cable(env, circuit, dev, iface)
    assert out is None
    assert "site" in err
