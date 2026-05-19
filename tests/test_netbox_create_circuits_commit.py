"""netbox_create_circuits get_or_create_circuit commit_rate branches."""

from unittest.mock import patch

import pytest
import requests

from netbox_create_circuits import get_or_create_circuit
from tests.mocks.netbox_full import NetBoxTestEnvironment


def test_get_or_create_circuit_clear_commit(monkeypatch):
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    prov, _ = env._ensure_provider("Cogent")
    ct = env.circuits.circuit_types._items[0]
    circuit = env.circuits.circuits.create(
        cid="CLR-1", provider=prov.id, type=ct.id, commit_rate=5000
    )
    with patch("netbox_create_circuits._patch_circuit_commit_rate", lambda *a, **k: setattr(circuit, "commit_rate", None)):
        c, msg = get_or_create_circuit(
            env, "CLR-1", prov, ct, commit_rate_kbps=None, clear_null_commit=True
        )
    assert c is not None
    assert "cleared" in (msg or "")


def test_get_or_create_circuit_patch_error(capsys, monkeypatch):
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    prov, _ = env._ensure_provider("Cogent")
    ct = env.circuits.circuit_types._items[0]
    circuit = env.circuits.circuits.create(
        cid="ERR-1", provider=prov.id, type=ct.id, commit_rate=1000
    )

    def boom(*a, **k):
        raise RuntimeError("patch failed")

    monkeypatch.setattr("netbox_create_circuits._patch_circuit_commit_rate", boom)
    c, msg = get_or_create_circuit(env, "ERR-1", prov, ct, commit_rate_kbps=9999)
    assert c is not None
    assert msg is None
    assert "patch failed" in capsys.readouterr().err or "Error" in capsys.readouterr().err


def test_patch_circuit_no_token(monkeypatch):
    from netbox_create_circuits import _patch_circuit_commit_rate

    nb = type("NB", (), {"base_url": "", "token": ""})()
    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="base_url"):
        _patch_circuit_commit_rate(nb, 1, 1000)
