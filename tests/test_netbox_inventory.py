"""Unit tests for uplinks.netbox.inventory (read-only provider/circuit walk)."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from tests.mocks.netbox_api import MockNetBox, _Record
from uplinks.netbox import inventory as inv


def _provider(name="Cogent", provider_id=1):
    return _Record(id=provider_id, name=name)


def _circuit(cid="CKT-1", circuit_id=100, provider_id=1, commit_rate=10000000, status="active"):
    return _Record(
        id=circuit_id,
        cid=cid,
        provider_id=provider_id,
        provider=provider_id,
        commit_rate=commit_rate,
        status=status,
    )


def _build_inventory_nb(
    *,
    provider=None,
    circuit=None,
    device=None,
    iface=None,
    ct=None,
    cable=None,
    providers=None,
    circuits=None,
):
    provider = provider or _provider()
    circuit = circuit or _circuit()
    device = device or _Record(id=1, name="ALA-KZT-7280TR-1", tag="border")
    iface = iface or _Record(id=10, name="Ethernet51/1", device=device, device_id=1)
    ct = ct or _Record(
        id=1,
        term_side="A",
        cable=_Record(id=50),
        circuit=circuit,
        circuit_id=circuit.id,
    )
    cable = cable or _Record(
        id=50,
        a_terminations=[{"object_type": "circuits.circuittermination", "object_id": ct.id}],
        b_terminations=[{"object_type": "dcim.interface", "object_id": iface.id}],
    )

    class _Providers:
        def all(self):
            return providers or [provider]

        def filter(self, **kwargs):
            return providers or [provider]

        def get(self, pk):
            for p in providers or [provider]:
                if p.id == pk:
                    return p
            return None

    class _CircuitsEndpoint:
        def __init__(self, items):
            self._items = items

        def filter(self, **kwargs):
            out = self._items
            if "provider_id" in kwargs:
                out = [c for c in out if getattr(c, "provider_id", None) == kwargs["provider_id"]]
            return out

        def get(self, pk):
            for c in self._items:
                if c.id == pk:
                    return c
            return None

    nb = MockNetBox(
        devices=[device],
        interfaces=[iface],
        cables=[cable],
        terminations=[ct],
        circuits=circuits or [circuit],
    )
    nb.circuits.providers = _Providers()
    nb.circuits.circuits = _CircuitsEndpoint(circuits or [circuit])
    return nb


def test_collect_full_path():
    nb = _build_inventory_nb()
    report = inv.collect_uplink_inventory(nb, tag="border", debug=True)
    assert len(report["complete"]) == 1
    row = report["complete"][0]
    assert row["provider"] == "Cogent"
    assert row["circuit_id"] == "CKT-1"
    assert row["device"] == "ALA-KZT-7280TR-1"
    assert row["interface"] == "Ethernet51/1"
    assert row["commit_rate_kbps"] == 10000000
    assert report["incomplete"] == []


def test_incomplete_no_cable():
    circuit = _circuit()
    ct = _Record(id=1, term_side="A", cable=None, circuit=circuit, circuit_id=circuit.id)
    nb = _build_inventory_nb(circuit=circuit, ct=ct, cable=None)
    nb.dcim.cables = type("C", (), {"get": lambda self, pk: None})()
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_NO_CABLE


def test_incomplete_not_border_device():
    device = _Record(id=1, name="core-switch", tag="core")
    iface = _Record(id=10, name="Eth1", device=device, device_id=1)
    nb = _build_inventory_nb(device=device, iface=iface)
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_NOT_BORDER_DEVICE
    assert report["incomplete"][0]["device"] == "core-switch"


def test_complete_with_missing_commit_rate_warning():
    circuit = _circuit(commit_rate=None)
    nb = _build_inventory_nb(circuit=circuit)
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert len(report["complete"]) == 1
    assert report["complete"][0]["commit_rate_kbps"] is None
    assert inv.REASON_MISSING_COMMIT_RATE in report["complete"][0]["warnings"]


def test_inventory_is_read_only_no_mutations():
    nb = _build_inventory_nb()
    for endpoint_name in ("providers", "circuits", "circuit_terminations"):
        endpoint = getattr(nb.circuits, endpoint_name)
        endpoint.create = MagicMock(side_effect=AssertionError("create must not be called"))
        endpoint.delete = MagicMock(side_effect=AssertionError("delete must not be called"))
        if hasattr(endpoint, "update"):
            endpoint.update = MagicMock(side_effect=AssertionError("update must not be called"))
    nb.dcim.cables.create = MagicMock(side_effect=AssertionError("create must not be called"))
    nb.dcim.cables.delete = MagicMock(side_effect=AssertionError("delete must not be called"))
    nb.dcim.interfaces.create = MagicMock(side_effect=AssertionError("create must not be called"))
    nb.dcim.devices.create = MagicMock(side_effect=AssertionError("create must not be called"))

    inv.collect_uplink_inventory(nb, tag="border", debug=True)


def test_main_json_dry_run(monkeypatch, netbox_env, capsys):
    nb = _build_inventory_nb()
    with patch.object(inv.pynetbox, "api", lambda url, token: nb):
        monkeypatch.setattr(
            sys,
            "argv",
            ["netbox_uplinks_inventory.py", "--json", "--dry-run", "--debug"],
        )
        assert inv.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["read_only"] is True
    assert len(payload["complete"]) == 1


def _choice_record(value, label=None):
    return _Record(value=value, label=label or value)


def test_termination_side_a_choice_record():
    circuit = _circuit()
    ct = _Record(
        id=1,
        term_side=_choice_record("A", "Side A"),
        cable=_Record(id=50),
        circuit=circuit,
        circuit_id=circuit.id,
    )
    device = _Record(id=1, name="R1", tag="border")
    iface = _Record(id=10, name="Eth1", device=device, device_id=1)
    cable = _Record(
        id=50,
        a_terminations=[{"object_type": "circuits.circuittermination", "object_id": ct.id}],
        b_terminations=[{"object_type": "dcim.interface", "object_id": iface.id}],
    )
    nb = _build_inventory_nb(circuit=circuit, device=device, iface=iface, ct=ct, cable=cable)
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert len(report["complete"]) == 1


def test_json_serializes_choice_status(monkeypatch, netbox_env, capsys):
    circuit = _circuit(status=_choice_record("active", "Active"))
    nb = _build_inventory_nb(circuit=circuit)
    report = inv.collect_uplink_inventory(nb, tag="border")
    payload = dict(report)
    payload["dry_run"] = False
    payload["read_only"] = True
    encoded = json.dumps(payload, indent=2, ensure_ascii=False)
    decoded = json.loads(encoded)
    assert decoded["complete"][0]["status"] == "active"

    with patch.object(inv.pynetbox, "api", lambda url, token: nb):
        monkeypatch.setattr(sys, "argv", ["netbox_uplinks_inventory.py", "--json"])
        assert inv.main() == 0
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_payload["complete"][0]["status"] == "active"


def test_providers_unavailable_exit_code(monkeypatch, netbox_env, capsys):
    class _BrokenProviders:
        def all(self):
            raise RuntimeError("api down")

        def filter(self, **kwargs):
            raise RuntimeError("api down")

    nb = _build_inventory_nb()
    nb.circuits.providers = _BrokenProviders()
    report = inv.collect_uplink_inventory(nb, tag="border", debug=True)
    assert report["stats"]["error"] == "providers_unavailable"
    assert report["complete"] == []

    with patch.object(inv.pynetbox, "api", lambda url, token: nb):
        monkeypatch.setattr(sys, "argv", ["netbox_uplinks_inventory.py"])
        assert inv.main() == 1
    err = capsys.readouterr()
    assert "providers unavailable" in err.err.lower()

    with patch.object(inv.pynetbox, "api", lambda url, token: nb):
        monkeypatch.setattr(sys, "argv", ["netbox_uplinks_inventory.py", "--json"])
        assert inv.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stats"]["error"] == "providers_unavailable"


def test_main_text_output_incomplete_exit_code(monkeypatch, netbox_env, capsys):
    circuit = _circuit()
    ct = _Record(id=1, term_side="A", cable=None, circuit=circuit, circuit_id=circuit.id)
    nb = _build_inventory_nb(circuit=circuit, ct=ct, cable=None)
    nb.dcim.cables = type("C", (), {"get": lambda self, pk: None})()
    with patch.object(inv.pynetbox, "api", lambda url, token: nb):
        monkeypatch.setattr(sys, "argv", ["netbox_uplinks_inventory.py"])
        assert inv.main() == 1
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out
    assert "no_cable" in out


def test_billing_model_choice_record_json():
    circuit = _circuit()
    circuit.custom_fields = {"billing_model": _choice_record("Burst", "Burst billing")}
    nb = _build_inventory_nb(circuit=circuit)
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["complete"][0]["billing_model"] == "Burst"
    payload = dict(report)
    encoded = json.dumps(payload, ensure_ascii=False)
    decoded = json.loads(encoded)
    assert decoded["complete"][0]["billing_model"] == "Burst"


def test_cable_non_string_object_type_incomplete():
    circuit = _circuit()
    ct = _Record(
        id=1,
        term_side="A",
        cable=_Record(id=50),
        circuit=circuit,
        circuit_id=circuit.id,
    )
    cable = _Record(
        id=50,
        a_terminations=[{"object_type": "circuits.circuittermination", "object_id": ct.id}],
        b_terminations=[{"object_type": 12345, "object_id": 10}],
    )
    nb = _build_inventory_nb(circuit=circuit, ct=ct, cable=cable)
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_CABLE_NOT_TO_INTERFACE


def test_inactive_planned_circuits_skipped():
    active = _circuit(cid="CKT-ACTIVE", circuit_id=100, status="active")
    planned = _circuit(cid="CKT-PLANNED", circuit_id=101, status=_choice_record("planned", "Planned"))
    inactive = _circuit(cid="CKT-INACTIVE", circuit_id=102, status=_choice_record("inactive", "Inactive"))

    device = _Record(id=1, name="R1", tag="border")
    iface = _Record(id=10, name="Eth1", device=device, device_id=1)

    def _ct_for(circ, ct_id):
        return _Record(
            id=ct_id,
            term_side="A",
            cable=_Record(id=50 + ct_id),
            circuit=circ,
            circuit_id=circ.id,
        )

    ct_active = _ct_for(active, 1)
    cables = [
        _Record(
            id=51,
            a_terminations=[{"object_type": "circuits.circuittermination", "object_id": ct_active.id}],
            b_terminations=[{"object_type": "dcim.interface", "object_id": iface.id}],
        ),
    ]
    terminations = [ct_active]

    nb = _build_inventory_nb(
        circuit=active,
        device=device,
        iface=iface,
        ct=ct_active,
        cable=cables[0],
        circuits=[active, planned, inactive],
    )
    nb.circuits.circuit_terminations = type(
        "Terms",
        (),
        {
            "filter": lambda self, **kw: [
                t for t in terminations if getattr(t, "circuit_id", None) == kw.get("circuit_id")
            ],
        },
    )()

    report = inv.collect_uplink_inventory(nb, tag="border")
    assert len(report["complete"]) == 1
    assert report["complete"][0]["circuit_id"] == "CKT-ACTIVE"
    assert report["incomplete"] == []
    assert report["stats"]["circuits_seen"] == 3
    assert report["stats"]["circuits_active"] == 1
