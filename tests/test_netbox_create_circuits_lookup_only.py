"""netbox_create_circuits default lookup-only mode (no NetBox mutations)."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from netbox_create_circuits import (
    _cable_connects_to_interface,
    find_circuit,
    find_circuit_type,
    find_provider,
    main,
    verify_termination_and_cable,
)
from tests.mocks.netbox_api import _Record, build_netbox_for_commit_rates
from tests.mocks.netbox_full import NetBoxTestEnvironment

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_find_provider_missing_reports_error():
    nb = NetBoxTestEnvironment()
    prov, err = find_provider(nb, "MissingISP")
    assert prov is None
    assert "provider not found" in err


def test_find_provider_existing():
    nb = NetBoxTestEnvironment()
    created, _ = nb._ensure_provider("Cogent")
    prov, err = find_provider(nb, "Cogent")
    assert err is None
    assert prov.id == created.id


def test_find_circuit_type_missing():
    nb = NetBoxTestEnvironment()
    ct, err = find_circuit_type(nb, "Internet")
    assert ct is None
    assert "circuit type not found" in err


def test_find_circuit_missing():
    nb = NetBoxTestEnvironment()
    prov, _ = nb._ensure_provider("Cogent")
    circuit, err = find_circuit(nb, "CKT-404", prov)
    assert circuit is None
    assert "circuit not found" in err


def test_verify_termination_and_cable_complete_inventory():
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        device_tag="border",
        tag_device=True,
    )
    dev = nb.dcim.devices._items[0]
    iface = nb.dcim.interfaces._items[0]
    circuit = nb.circuits.circuits._items[0]
    term, err = verify_termination_and_cable(nb, circuit, dev, iface)
    assert err is None
    assert term is not None


def test_verify_termination_and_cable_missing_termination():
    env = NetBoxTestEnvironment()
    dev = env.add_device("R1")
    dev.site = 1
    iface = env.add_interface(dev, "Ethernet1/1")
    prov, _ = env._ensure_provider("Cogent")
    ct = env.circuits.circuit_types.create(name="Internet", slug="internet")
    circuit = env.circuits.circuits.create(cid="NO-TERM", provider=prov.id, type=ct.id)
    term, err = verify_termination_and_cable(env, circuit, dev, iface)
    assert term is None
    assert "termination not found" in err


def test_verify_termination_and_cable_missing_cable_reference():
    env = NetBoxTestEnvironment()
    dev = env.add_device("R1")
    dev.site = 1
    iface = env.add_interface(dev, "Ethernet1/1")
    prov, _ = env._ensure_provider("Cogent")
    ct_type = env.circuits.circuit_types.create(name="Internet", slug="internet")
    circuit = env.circuits.circuits.create(cid="NO-CABLE", provider=prov.id, type=ct_type.id)
    env.circuits.circuit_terminations.create(
        circuit=circuit.id,
        term_side="A",
        cable=None,
    )
    term, err = verify_termination_and_cable(env, circuit, dev, iface)
    assert term is not None
    assert "cable not found" in err


def test_verify_termination_and_cable_missing_cable_record():
    env = NetBoxTestEnvironment()
    dev = env.add_device("R1")
    dev.site = 1
    iface = env.add_interface(dev, "Ethernet1/1", iface_id=10)
    prov, _ = env._ensure_provider("Cogent")
    ct_type = env.circuits.circuit_types.create(name="Internet", slug="internet")
    circuit = env.circuits.circuits.create(cid="MISSING-REC", provider=prov.id, type=ct_type.id)
    term = env.circuits.circuit_terminations.create(
        circuit=circuit.id,
        term_side="A",
        cable=type("CableRef", (), {"id": 999})(),
    )
    result_term, err = verify_termination_and_cable(env, circuit, dev, iface)
    assert result_term.id == term.id
    assert "cable record not found" in err
    assert "999" in err


def test_cable_connects_rejects_colliding_non_interface_object_type():
    nb = build_netbox_for_commit_rates(
        device_name="R1",
        iface_name="Ethernet51/1",
        iface_id=10,
        provider_name="Cogent",
        cable_id=50,
        ct_id=10,
    )
    cable = nb.dcim.cables._items[0]
    cable.b_terminations = [
        {"object_type": "circuits.circuittermination", "object_id": 10},
    ]
    assert _cable_connects_to_interface(nb, 50, 10, circuit_termination_id=1) is False
    term, err = verify_termination_and_cable(
        nb,
        nb.circuits.circuits._items[0],
        nb.dcim.devices._items[0],
        nb.dcim.interfaces._items[0],
    )
    assert "does not connect" in err


def test_verify_termination_and_cable_wrong_interface():
    nb = build_netbox_for_commit_rates(
        device_name="R1",
        iface_name="Ethernet51/1",
        iface_id=10,
        provider_name="Cogent",
        cable_id=50,
    )
    other_iface = _Record(id=99, name="Ethernet99/1", device=nb.dcim.devices._items[0])
    term, err = verify_termination_and_cable(
        nb,
        nb.circuits.circuits._items[0],
        nb.dcim.devices._items[0],
        other_iface,
    )
    assert term is not None
    assert "does not connect" in err
    assert "Ethernet99/1" in err


def test_verify_termination_and_cable_pass_through_ports():
    nb = build_netbox_for_commit_rates(
        device_name="MIA-EQX-7280QR-1",
        iface_name="Ethernet23/1",
        iface_id=10000,
        provider_name="Cogent",
        cable_id=7564,
        ct_id=44,
        circuit_id=100,
    )
    dev = nb.dcim.devices._items[0]
    iface = nb.dcim.interfaces._items[0]
    iface.url = "https://netbox.example/api/dcim/interfaces/{}/".format(iface.id)
    circuit = nb.circuits.circuits._items[0]
    ct = nb.circuits.circuit_terminations._items[0]
    ct.url = "https://netbox.example/api/circuits/circuit-terminations/{}/".format(ct.id)
    odf_device = _Record(id=99, name="MIA-ODF-1")
    rear_port = _Record(
        id=5,
        name="Trunk-1",
        device=odf_device,
        url="https://netbox.example/api/dcim/rear-ports/5/",
    )
    front_port = _Record(
        id=121,
        name="Port-1",
        device=odf_device,
        url="https://netbox.example/api/dcim/front-ports/121/",
    )
    cable_to_iface = _Record(
        id=6615,
        url="https://netbox.example/api/dcim/cables/6615/",
    )
    cable = nb.dcim.cables._items[0]
    cable.url = "https://netbox.example/api/dcim/cables/{}/".format(cable.id)
    cable.b_terminations = [{"object_type": "dcim.rearport", "object_id": rear_port.id}]
    rear_port.paths = MagicMock(
        return_value=[
            {
                "origin": iface,
                "destination": ct,
                "path": [
                    [
                        iface,
                        cable_to_iface,
                        front_port,
                        rear_port,
                        cable,
                        ct,
                    ]
                ],
            }
        ]
    )

    class _RearPorts:
        def get(self, pk):
            if pk == rear_port.id:
                return rear_port
            return None

    nb.dcim.rear_ports = _RearPorts()
    term, err = verify_termination_and_cable(nb, circuit, dev, iface)
    assert err is None
    assert term is not None
    rear_port.paths.assert_called_once()


def test_lookup_helpers_do_not_mutate(monkeypatch):
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        device_tag="border",
        tag_device=True,
    )
    dev = nb.dcim.devices._items[0]
    iface = nb.dcim.interfaces._items[0]
    circuit = nb.circuits.circuits._items[0]
    provider = nb.circuits.providers._providers[0]

    def fail_update(self, data):
        raise AssertionError("Record.update called in lookup-only path: {}".format(data))

    def fail_patch(*args, **kwargs):
        raise AssertionError("requests.patch called in lookup-only path")

    monkeypatch.setattr(_Record, "update", fail_update)
    monkeypatch.setattr("uplinks.netbox.circuits.requests.patch", fail_patch)

    prov, err = find_provider(nb, provider.name)
    assert err is None and prov is not None
    found, err = find_circuit(nb, circuit.cid, prov)
    assert err is None and found is not None
    term, err = verify_termination_and_cable(nb, circuit, dev, iface)
    assert err is None


def test_main_default_mode_no_mutations(monkeypatch, tmp_path, capsys):
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    dev = env.dcim.devices._items[0]
    dev.tag = "border"
    dev.site = 1

    create_calls = {
        "providers": 0,
        "circuit_types": 0,
        "circuits": 0,
        "terminations": 0,
        "cables": 0,
        "tags": 0,
    }
    orig_provider_create = env.circuits.providers.create
    orig_type_create = env.circuits.circuit_types.create
    orig_circuit_create = env.circuits.circuits.create
    orig_term_create = env.circuits.circuit_terminations.create
    orig_cable_create = env.dcim.cables.create
    orig_tag_create = env.extras.tags.create

    def track_create(endpoint, key):
        def wrapper(**kwargs):
            create_calls[key] += 1
            return endpoint(**kwargs)

        return wrapper

    env.circuits.providers.create = track_create(orig_provider_create, "providers")
    env.circuits.circuit_types.create = track_create(orig_type_create, "circuit_types")
    env.circuits.circuits.create = track_create(orig_circuit_create, "circuits")
    env.circuits.circuit_terminations.create = track_create(orig_term_create, "terminations")
    env.dcim.cables.create = track_create(orig_cable_create, "cables")
    env.extras.tags.create = track_create(orig_tag_create, "tags")
    env.dcim.cables.delete = lambda ids: (_ for _ in ()).throw(AssertionError("delete called"))

    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "ALA-KZT-7280TR-1": {
                    "Ethernet51/1": {
                        "provider": "Cogent",
                        "circuit_id": "CKT-LOOKUP-1",
                        "commit_rate_gbps": 10,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    with patch("netbox_create_circuits.pynetbox.api", lambda url, token: env):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "netbox_create_circuits.py",
                "-f",
                str(cr),
                "-d",
                str(FIXTURES / "dry_ssh_minimal.json"),
            ],
        )
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1
    assert all(count == 0 for count in create_calls.values())
    err = capsys.readouterr().err
    assert "provider not found" in err or "circuit type not found" in err


def test_main_default_mode_finds_existing_chain(monkeypatch, tmp_path, capsys):
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    dev = env.dcim.devices._items[0]
    dev.tag = "border"
    dev.site = 1
    iface = env.dcim.interfaces._items[0]
    prov, _ = env._ensure_provider("ManualISP")
    ct = env.circuits.circuit_types._items[0]
    circuit = env.circuits.circuits.create(
        cid="CKT-MANUAL-1", provider=prov.id, type=ct.id, commit_rate=10_000_000
    )
    term = env.circuits.circuit_terminations.create(
        circuit=circuit.id,
        term_side="A",
        cable=type("CableRef", (), {"id": 50})(),
    )
    env.add_cable(
        50,
        {"object_type": "circuits.circuittermination", "object_id": term.id},
        {"object_type": "dcim.interface", "object_id": iface.id},
    )

    create_calls = {"providers": 0, "circuits": 0, "cables": 0}
    orig_provider_create = env.circuits.providers.create
    orig_circuit_create = env.circuits.circuits.create
    orig_cable_create = env.dcim.cables.create

    def track(endpoint, key):
        def wrapper(**kwargs):
            create_calls[key] += 1
            return endpoint(**kwargs)

        return wrapper

    env.circuits.providers.create = track(orig_provider_create, "providers")
    env.circuits.circuits.create = track(orig_circuit_create, "circuits")
    env.dcim.cables.create = track(orig_cable_create, "cables")

    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "ALA-KZT-7280TR-1": {
                    "Ethernet51/1": {
                        "provider": "ManualISP",
                        "circuit_id": "CKT-MANUAL-1",
                        "commit_rate_gbps": 10,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    with patch("netbox_create_circuits.pynetbox.api", lambda url, token: env):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "netbox_create_circuits.py",
                "-f",
                str(cr),
                "-d",
                str(FIXTURES / "dry_ssh_minimal.json"),
            ],
        )
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 0
    assert all(count == 0 for count in create_calls.values())
    out = capsys.readouterr().out
    assert "OK:" in out
    assert "(lookup)" in out
