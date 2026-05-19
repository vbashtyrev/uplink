"""Push netbox_create_circuits and netbox_uplinks_cleanup to >=85%."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

import netbox_create_circuits as ncc
import netbox_uplinks_cleanup as nuc
from netbox_create_circuits import (
    get_or_create_circuit,
    get_or_create_circuit_type,
    get_or_create_provider,
    main as circuits_main,
)
from netbox_uplinks_cleanup import _get_nb, cleanup_circuits, main as cleanup_main
from tests.mocks.netbox_full import NetBoxTestEnvironment
from tests.mocks.netbox_records import MutableNetBox, _Record

from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_get_or_create_provider_and_type_create():
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    p, msg = get_or_create_provider(env, "NewISP")
    assert p is not None
    assert msg == "created"
    ct, msg2 = get_or_create_circuit_type(env, "Transit-New")
    assert ct is not None
    assert msg2 == "created"


def test_get_or_create_circuit_clear_null_commit(monkeypatch):
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    prov, _ = env._ensure_provider("Cogent")
    ct = env.circuits.circuit_types._items[0]
    circuit = env.circuits.circuits.create(
        cid="CLR-1", provider=prov.id, type=ct.id, commit_rate=10_000_000
    )
    monkeypatch.setattr(
        ncc, "_patch_circuit_commit_rate", lambda nb, cid, rate: setattr(circuit, "commit_rate", rate)
    )
    c, msg = get_or_create_circuit(
        env, "CLR-1", prov, ct, None, clear_null_commit=True
    )
    assert c is not None
    assert "cleared" in msg


def test_get_or_create_automation_tag_create_fails(monkeypatch):
    nb = MutableNetBox()

    class Tags:
        def get(self, name=None, slug=None):
            return None

        def create(self, **kwargs):
            raise RuntimeError("tag fail")

    nb.extras.tags = Tags()
    assert ncc._get_or_create_automation_tag(nb) is None


def test_get_or_create_provider_create_error():
    nb = MutableNetBox()

    class Prov:
        def filter(self, **kwargs):
            return []

        def create(self, **kwargs):
            raise RuntimeError("prov fail")

    nb.circuits.providers = Prov()
    p, msg = get_or_create_provider(nb, "FailISP")
    assert p is None
    assert "prov fail" in msg


def test_patch_circuit_url_without_api_suffix(monkeypatch):
    nb = MagicMock()
    nb.base_url = "https://nb.example"
    nb.token = "tok"
    calls = []
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    monkeypatch.setattr(
        requests,
        "patch",
        lambda url, **kwargs: calls.append(url) or resp,
    )
    ncc._patch_circuit_commit_rate(nb, 5, 1000)
    assert "/api/circuits/circuits/5/" in calls[0]


def test_get_or_create_automation_tag_missing():
    with patch.object(ncc, "AUTOMATION_TAG", ""):
        assert ncc._get_or_create_automation_tag(MutableNetBox()) is None


def test_create_termination_netbox32_fallback():
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    dev = env.dcim.devices._items[0]
    dev.site = 1
    iface = env.add_interface(dev, "Ethernet51/1")
    prov, _ = env._ensure_provider("Cogent")
    ct_type = env.circuits.circuit_types._items[0]
    circuit = env.circuits.circuits.create(
        cid="NB32", provider=prov.id, type=ct_type.id, commit_rate=10_000_000
    )

    calls = []

    class Terms:
        def filter(self, **kwargs):
            return []

        def create(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("termination_type"):
                raise RuntimeError("4.2 only")
            return _Record(id=99, circuit=circuit.id, term_side="A")

    env.circuits.circuit_terminations = Terms()
    with patch("netbox_create_circuits._patch_interface_mark_connected", lambda *a, **k: None):
        out, err = ncc.create_termination_and_cable(env, circuit, dev, iface)
    assert err is None
    assert len(calls) == 2


def test_main_empty_circuit_id(monkeypatch, netbox_env, tmp_path, capsys):
    monkeypatch.setenv("NETBOX_TAG", "automatization")
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    dev = env.dcim.devices._items[0]
    dev.name = "ALA-KZT-7280TR-1"
    cr = tmp_path / "cr.json"
    cr.write_text(
        json.dumps(
            {
                "ALA-KZT-7280TR-1": {
                    "Ethernet51/1": {"provider": "Cogent", "circuit_id": ""},
                },
            }
        ),
        encoding="utf-8",
    )
    with patch("netbox_create_circuits.pynetbox.api", lambda url, token: env):
        monkeypatch.setattr(
            sys,
            "argv",
            ["netbox_create_circuits.py", "-f", str(cr)],
        )
        with pytest.raises(SystemExit) as exc:
            circuits_main()
        assert exc.value.code == 1
    assert "empty circuit_id" in capsys.readouterr().err


def test_get_nb_missing_env(monkeypatch):
    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        _get_nb()


def test_cleanup_circuits_filter_error(capsys):
    nb = MagicMock()
    nb.circuits.circuits.filter.side_effect = RuntimeError("circ filter")
    n, ids = cleanup_circuits(nb, "auto", debug=True)
    assert n == 0 and ids == []
    assert "circ filter" in capsys.readouterr().err


def test_cleanup_cables_empty_ids():
    nb = MagicMock()
    nb.dcim.cables.filter.return_value = [_Record(id=None)]
    assert nuc.cleanup_cables(nb, "auto") == 0
