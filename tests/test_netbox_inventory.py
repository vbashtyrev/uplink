"""Unit tests for uplinks.netbox.inventory (read-only provider/circuit walk)."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pynetbox.core.query import RequestError

from tests.mocks.netbox_api import MockNetBox, _Record, add_project_circuit_scope
from uplinks.netbox import inventory as inv


def _netbox_request_error(status, url_path):
    req = SimpleNamespace(
        status_code=status,
        url="https://netbox.example{}".format(url_path),
        reason={
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
        }.get(status, "Error"),
        request=SimpleNamespace(body=None),
        json=lambda: {},
        text="",
    )
    return RequestError(req)


def _provider(name="Cogent", provider_id=1):
    return _Record(id=provider_id, name=name)


def _scoped_circuit(cid="CKT-1", circuit_id=100, provider_id=1, commit_rate=10000000, status="active"):
    circuit = _Record(
        id=circuit_id,
        cid=cid,
        provider_id=provider_id,
        provider=provider_id,
        commit_rate=commit_rate,
        status=status,
    )
    return add_project_circuit_scope(circuit)


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
            if "tag" in kwargs:
                out = [c for c in out if getattr(c, "tag", None) == kwargs["tag"]]
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


def _flat_agg_cap_circuit(cid="KZT-ALA-1", circuit_id=100, provider_id=1, commit_rate=None):
    circuit = _circuit(
        cid=cid,
        circuit_id=circuit_id,
        provider_id=provider_id,
        commit_rate=commit_rate,
    )
    circuit.custom_fields = {"billing_model": "FlatAggCap"}
    return circuit


def test_flat_agg_cap_with_provider_aggregate_limit_no_commit_warning():
    provider = _provider(name="KZT", provider_id=1)
    provider.custom_fields = {"aggregate_limit_gbps": 12}
    circuit = _flat_agg_cap_circuit()
    nb = _build_inventory_nb(provider=provider, circuit=circuit)
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert len(report["complete"]) == 1
    row = report["complete"][0]
    assert row["commit_rate_kbps"] is None
    assert row["billing_model"] == "FlatAggCap"
    assert "warnings" not in row


def test_flat_agg_cap_without_provider_aggregate_limit_warns():
    provider = _provider(name="KZT", provider_id=1)
    circuit = _flat_agg_cap_circuit()
    nb = _build_inventory_nb(provider=provider, circuit=circuit)
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert len(report["complete"]) == 1
    row = report["complete"][0]
    assert row["commit_rate_kbps"] is None
    assert inv.REASON_MISSING_PROVIDER_AGGREGATE_LIMIT in row["warnings"]
    assert inv.REASON_MISSING_COMMIT_RATE not in row["warnings"]


def test_flat_agg_cap_invalid_provider_aggregate_limit_warns():
    provider = _provider(name="KZT", provider_id=1)
    provider.custom_fields = {"aggregate_limit_gbps": "not-a-number"}
    circuit = _flat_agg_cap_circuit()
    nb = _build_inventory_nb(provider=provider, circuit=circuit)
    report = inv.collect_uplink_inventory(nb, tag="border")
    row = report["complete"][0]
    assert inv.REASON_MISSING_PROVIDER_AGGREGATE_LIMIT in row["warnings"]


def test_non_flat_agg_cap_missing_commit_rate_warning_unchanged():
    circuit = _circuit(commit_rate=None)
    circuit.custom_fields = {"billing_model": "Flat"}
    nb = _build_inventory_nb(circuit=circuit)
    report = inv.collect_uplink_inventory(nb, tag="border")
    row = report["complete"][0]
    assert inv.REASON_MISSING_COMMIT_RATE in row["warnings"]
    assert inv.REASON_MISSING_PROVIDER_AGGREGATE_LIMIT not in row.get("warnings", [])


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
    nb = _build_inventory_nb(circuit=_scoped_circuit())
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
    provider = _provider()
    circuit = add_project_circuit_scope(
        _circuit(status=_choice_record("active", "Active"), provider_id=provider.id)
    )
    circuit.provider = provider.id
    nb = _build_inventory_nb(provider=provider, circuit=circuit)
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
    assert report["stats"]["error"] == inv.ERROR_PROVIDERS_UNAVAILABLE
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
    assert payload["stats"]["error"] == inv.ERROR_PROVIDERS_UNAVAILABLE


def test_main_text_output_incomplete_exit_code(monkeypatch, netbox_env, capsys):
    provider = _provider()
    circuit = _scoped_circuit(provider_id=provider.id)
    circuit.provider = provider.id
    ct = _Record(id=1, term_side="A", cable=None, circuit=circuit, circuit_id=circuit.id)
    nb = _build_inventory_nb(provider=provider, circuit=circuit, ct=ct, cable=None)
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


def test_active_only_excludes_empty_and_unknown_status():
    active = _circuit(cid="CKT-ACTIVE", circuit_id=100, status="active")
    no_status = _circuit(cid="CKT-NO-STATUS", circuit_id=101, status=None)
    empty_status = _circuit(cid="CKT-EMPTY", circuit_id=102, status="")
    unknown = _circuit(cid="CKT-UNK", circuit_id=103, status="provisioned")

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
    terminations = [ct_active]
    cables = [
        _Record(
            id=51,
            a_terminations=[{"object_type": "circuits.circuittermination", "object_id": ct_active.id}],
            b_terminations=[{"object_type": "dcim.interface", "object_id": iface.id}],
        ),
    ]

    nb = _build_inventory_nb(
        circuit=active,
        device=device,
        iface=iface,
        ct=ct_active,
        cable=cables[0],
        circuits=[active, no_status, empty_status, unknown],
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
    assert report["stats"]["circuits_seen"] == 4
    assert report["stats"]["circuits_active"] == 1


def test_auth_denied_on_providers():
    nb = _build_inventory_nb()
    nb.circuits.providers = type(
        "P",
        (),
        {
            "all": lambda self: (_ for _ in ()).throw(Exception("403 Forbidden")),
            "filter": lambda self, **kw: (_ for _ in ()).throw(Exception("403 Forbidden")),
        },
    )()
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["stats"]["error"] == inv.ERROR_AUTH_DENIED
    assert report["complete"] == []


def test_auth_denied_on_devices_filter():
    nb = _build_inventory_nb()
    nb.dcim.devices.filter = lambda **kw: (_ for _ in ()).throw(Exception("token expired"))
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["stats"]["error"] == inv.ERROR_AUTH_DENIED


def test_auth_denied_on_cables_get():
    nb = _build_inventory_nb()
    nb.dcim.cables.get = lambda pk: (_ for _ in ()).throw(Exception("403 forbidden"))
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["stats"]["error"] == inv.ERROR_AUTH_DENIED


def test_main_auth_exit_code(monkeypatch, netbox_env, capsys):
    nb = _build_inventory_nb(circuit=_scoped_circuit())
    nb.circuits.providers = type(
        "P",
        (),
        {
            "all": lambda self: (_ for _ in ()).throw(Exception("403 Forbidden")),
            "filter": lambda self, **kw: [],
        },
    )()
    with patch.object(inv.pynetbox, "api", lambda url, token: nb):
        monkeypatch.setattr(sys, "argv", ["netbox_uplinks_inventory.py", "--json"])
        assert inv.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stats"]["error"] == inv.ERROR_AUTH_DENIED


def test_is_netbox_auth_error_404_object_id_403_not_auth():
    exc = _netbox_request_error(404, "/api/dcim/cables/403/")
    assert inv.is_netbox_auth_error(exc) is False
    assert inv.is_netbox_auth_error(Exception(str(exc))) is False


def test_is_netbox_auth_error_401_is_auth():
    exc = _netbox_request_error(401, "/api/circuits/providers/")
    assert inv.is_netbox_auth_error(exc) is True
    assert inv.is_netbox_auth_error(Exception("The request failed with code 401 Unauthorized: {}")) is True


def _netbox44_path_segment(
    border_iface,
    cable_to_iface,
    front_port,
    rear_port,
    cable_to_rear,
    ct,
):
    """NetBox 4.4 flat path: Interface, Cable, FrontPort, RearPort, Cable, CircuitTermination."""
    return [
        border_iface,
        cable_to_iface,
        front_port,
        rear_port,
        cable_to_rear,
        ct,
    ]


def _build_odf_inventory_nb(
    *,
    provider=None,
    circuit=None,
    border_device=None,
    border_iface=None,
    odf_device=None,
    rear_port=None,
    front_port=None,
    ct=None,
    cable_to_rear=None,
    cable_to_iface=None,
    paths_result=None,
):
    """Circuit Termination -> cable -> rearport -> frontport -> cable -> interface."""
    provider = provider or _provider(name="Cogent")
    circuit = circuit or _circuit(cid="Cogent-MIA-1", circuit_id=100)
    border_device = border_device or _Record(id=1, name="MIA-EQX-7280QR-1", tag="border")
    border_iface = border_iface or _Record(
        id=10000,
        name="Ethernet23/1",
        device=border_device,
        device_id=border_device.id,
        url="https://netbox.example/api/dcim/interfaces/10000/",
    )
    odf_device = odf_device or _Record(id=99, name="MIA-ODF-1")
    rear_port = rear_port or _Record(
        id=5,
        name="Trunk-1",
        device=odf_device,
        url="https://netbox.example/api/dcim/rear-ports/5/",
    )
    front_port = front_port or _Record(
        id=121,
        name="Port-1",
        device=odf_device,
        url="https://netbox.example/api/dcim/front-ports/121/",
    )
    ct = ct or _Record(
        id=44,
        term_side="A",
        cable=_Record(id=7564),
        circuit=circuit,
        circuit_id=circuit.id,
        url="https://netbox.example/api/circuits/circuit-terminations/44/",
    )
    cable_to_rear = cable_to_rear or _Record(
        id=7564,
        url="https://netbox.example/api/dcim/cables/7564/",
        a_terminations=[{"object_type": "circuits.circuittermination", "object_id": ct.id}],
        b_terminations=[{"object_type": "dcim.rearport", "object_id": rear_port.id}],
    )
    cable_to_iface = cable_to_iface or _Record(
        id=6615,
        url="https://netbox.example/api/dcim/cables/6615/",
    )
    if paths_result is None:
        paths_result = [
            {
                "id": 1,
                "is_active": True,
                "is_complete": True,
                "is_split": False,
                "origin": border_iface,
                "destination": ct,
                "path": [
                    _netbox44_path_segment(
                        border_iface,
                        cable_to_iface,
                        front_port,
                        rear_port,
                        cable_to_rear,
                        ct,
                    )
                ],
            }
        ]

    rear_port.paths = MagicMock(return_value=paths_result)

    class _RearPorts:
        def get(self, pk):
            if pk == rear_port.id:
                return rear_port
            return None

    nb = _build_inventory_nb(
        provider=provider,
        circuit=circuit,
        device=border_device,
        iface=border_iface,
        ct=ct,
        cable=cable_to_rear,
    )
    nb.dcim.rear_ports = _RearPorts()
    return nb, rear_port


def test_collect_path_through_rear_front_ports():
    nb, rear_port = _build_odf_inventory_nb()
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert len(report["complete"]) == 1
    row = report["complete"][0]
    assert row["provider"] == "Cogent"
    assert row["circuit_id"] == "Cogent-MIA-1"
    assert row["device"] == "MIA-EQX-7280QR-1"
    assert row["interface"] == "Ethernet23/1"
    assert report["incomplete"] == []
    rear_port.paths.assert_called_once()


def test_incomplete_pass_through_path_without_interface():
    provider = _provider(name="Cogent")
    circuit = _circuit(cid="Cogent-MIA-1", circuit_id=100)
    odf_device = _Record(id=99, name="MIA-ODF-1")
    rear_port = _Record(
        id=5,
        name="Trunk-1",
        device=odf_device,
        url="https://netbox.example/api/dcim/rear-ports/5/",
    )
    ct = _Record(
        id=44,
        term_side="A",
        cable=_Record(id=7564),
        circuit=circuit,
        circuit_id=circuit.id,
    )
    cable_to_rear = _Record(
        id=7564,
        a_terminations=[{"object_type": "circuits.circuittermination", "object_id": ct.id}],
        b_terminations=[{"object_type": "dcim.rearport", "object_id": rear_port.id}],
    )
    front_port = _Record(
        id=121,
        name="Port-1",
        url="https://netbox.example/api/dcim/front-ports/121/",
    )
    rear_port.paths = MagicMock(
        return_value=[
            {
                "origin": None,
                "destination": None,
                "path": [[rear_port, cable_to_rear, front_port]],
            }
        ]
    )

    class _RearPorts:
        def get(self, pk):
            if pk == rear_port.id:
                return rear_port
            return None

    nb = _build_inventory_nb(circuit=circuit, ct=ct, cable=cable_to_rear)
    nb.dcim.rear_ports = _RearPorts()
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_CABLE_NOT_TO_INTERFACE
    rear_port.paths.assert_called_once()


def test_paths_called_read_only():
    nb, rear_port = _build_odf_inventory_nb()
    rear_port.create = MagicMock(side_effect=AssertionError("create must not be called"))
    rear_port.delete = MagicMock(side_effect=AssertionError("delete must not be called"))
    rear_port.update = MagicMock(side_effect=AssertionError("update must not be called"))
    inv.collect_uplink_inventory(nb, tag="border")
    rear_port.paths.assert_called_once()
    rear_port.create.assert_not_called()
    rear_port.delete.assert_not_called()
    rear_port.update.assert_not_called()


def test_interface_first_in_path_segment():
    """NetBox 4.4 may place the interface as the first element in a path segment."""
    nb, rear_port = _build_odf_inventory_nb()
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert len(report["complete"]) == 1
    assert report["complete"][0]["interface"] == "Ethernet23/1"
    assert report["complete"][0]["device"] == "MIA-EQX-7280QR-1"


def test_pass_through_non_border_device():
    core_device = _Record(id=2, name="core-switch", tag="core")
    core_iface = _Record(
        id=10000,
        name="Ethernet23/1",
        device=core_device,
        device_id=core_device.id,
        url="https://netbox.example/api/dcim/interfaces/10000/",
    )
    nb, rear_port = _build_odf_inventory_nb(border_iface=core_iface, border_device=core_device)
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_NOT_BORDER_DEVICE
    assert report["incomplete"][0]["device"] == "core-switch"
    assert report["incomplete"][0]["interface"] == "Ethernet23/1"
    rear_port.paths.assert_called_once()


def test_pass_through_selects_matching_path_among_multiple():
    border_device = _Record(id=1, name="MIA-EQX-7280QR-1", tag="border")
    border_iface = _Record(
        id=10000,
        name="Ethernet23/1",
        device=border_device,
        device_id=border_device.id,
        url="https://netbox.example/api/dcim/interfaces/10000/",
    )
    wrong_device = _Record(id=3, name="other-border", tag="border")
    wrong_iface = _Record(
        id=20000,
        name="Ethernet99/1",
        device=wrong_device,
        device_id=wrong_device.id,
        url="https://netbox.example/api/dcim/interfaces/20000/",
    )
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
    ct = _Record(
        id=44,
        term_side="A",
        cable=_Record(id=7564),
        circuit=_circuit(cid="Cogent-MIA-1"),
        circuit_id=100,
        url="https://netbox.example/api/circuits/circuit-terminations/44/",
    )
    cable_to_rear = _Record(
        id=7564,
        url="https://netbox.example/api/dcim/cables/7564/",
        a_terminations=[{"object_type": "circuits.circuittermination", "object_id": ct.id}],
        b_terminations=[{"object_type": "dcim.rearport", "object_id": rear_port.id}],
    )
    cable_to_iface = _Record(id=6615, url="https://netbox.example/api/dcim/cables/6615/")
    wrong_cable = _Record(id=9999, url="https://netbox.example/api/dcim/cables/9999/")
    wrong_ct = _Record(
        id=999,
        url="https://netbox.example/api/circuits/circuit-terminations/999/",
    )
    paths_result = [
        {
            "id": 1,
            "is_active": True,
            "is_complete": True,
            "is_split": False,
            "origin": wrong_iface,
            "destination": wrong_ct,
            "path": [[wrong_iface, wrong_cable, wrong_ct]],
        },
        {
            "id": 2,
            "is_active": True,
            "is_complete": True,
            "is_split": False,
            "origin": border_iface,
            "destination": ct,
            "path": [
                _netbox44_path_segment(
                    border_iface,
                    cable_to_iface,
                    front_port,
                    rear_port,
                    cable_to_rear,
                    ct,
                )
            ],
        },
    ]
    nb, rear_port = _build_odf_inventory_nb(
        border_device=border_device,
        border_iface=border_iface,
        rear_port=rear_port,
        front_port=front_port,
        ct=ct,
        cable_to_rear=cable_to_rear,
        cable_to_iface=cable_to_iface,
        paths_result=paths_result,
    )
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert len(report["complete"]) == 1
    assert report["complete"][0]["interface"] == "Ethernet23/1"
    assert report["complete"][0]["device"] == "MIA-EQX-7280QR-1"
    rear_port.paths.assert_called_once()


def _valid_odf_paths_result(nb_fixture):
    """Path that resolves to the border interface when is_complete/is_split allow it."""
    border_device = _Record(id=1, name="MIA-EQX-7280QR-1", tag="border")
    border_iface = _Record(
        id=10000,
        name="Ethernet23/1",
        device=border_device,
        device_id=border_device.id,
        url="https://netbox.example/api/dcim/interfaces/10000/",
    )
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
    circuit = _circuit(cid="Cogent-MIA-1", circuit_id=100)
    ct = _Record(
        id=44,
        term_side="A",
        cable=_Record(id=7564),
        circuit=circuit,
        circuit_id=circuit.id,
        url="https://netbox.example/api/circuits/circuit-terminations/44/",
    )
    cable_to_rear = _Record(
        id=7564,
        url="https://netbox.example/api/dcim/cables/7564/",
        a_terminations=[{"object_type": "circuits.circuittermination", "object_id": ct.id}],
        b_terminations=[{"object_type": "dcim.rearport", "object_id": rear_port.id}],
    )
    cable_to_iface = _Record(id=6615, url="https://netbox.example/api/dcim/cables/6615/")
    segment = _netbox44_path_segment(
        border_iface,
        cable_to_iface,
        front_port,
        rear_port,
        cable_to_rear,
        ct,
    )
    return {
        "border_iface": border_iface,
        "ct": ct,
        "cable_to_rear": cable_to_rear,
        "cable_to_iface": cable_to_iface,
        "rear_port": rear_port,
        "front_port": front_port,
        "segment": segment,
    }


def test_pass_through_incomplete_path_skipped():
    parts = _valid_odf_paths_result(None)
    paths_result = [
        {
            "id": 1,
            "is_active": True,
            "is_complete": False,
            "is_split": False,
            "origin": parts["border_iface"],
            "destination": parts["ct"],
            "path": [parts["segment"]],
        }
    ]
    nb, _rear_port = _build_odf_inventory_nb(
        paths_result=paths_result,
        border_iface=parts["border_iface"],
        ct=parts["ct"],
        cable_to_rear=parts["cable_to_rear"],
        cable_to_iface=parts["cable_to_iface"],
        rear_port=parts["rear_port"],
        front_port=parts["front_port"],
    )
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_CABLE_NOT_TO_INTERFACE


def test_pass_through_split_path_skipped():
    parts = _valid_odf_paths_result(None)
    paths_result = [
        {
            "id": 1,
            "is_active": True,
            "is_complete": True,
            "is_split": True,
            "origin": parts["border_iface"],
            "destination": parts["ct"],
            "path": [parts["segment"]],
        }
    ]
    nb, _rear_port = _build_odf_inventory_nb(
        paths_result=paths_result,
        border_iface=parts["border_iface"],
        ct=parts["ct"],
        cable_to_rear=parts["cable_to_rear"],
        cable_to_iface=parts["cable_to_iface"],
        rear_port=parts["rear_port"],
        front_port=parts["front_port"],
    )
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_CABLE_NOT_TO_INTERFACE


def test_pass_through_path_without_is_complete_key_skipped():
    parts = _valid_odf_paths_result(None)
    paths_result = [
        {
            "id": 1,
            "is_active": True,
            "is_split": False,
            "origin": parts["border_iface"],
            "destination": parts["ct"],
            "path": [parts["segment"]],
        }
    ]
    nb, _rear_port = _build_odf_inventory_nb(
        paths_result=paths_result,
        border_iface=parts["border_iface"],
        ct=parts["ct"],
        cable_to_rear=parts["cable_to_rear"],
        cable_to_iface=parts["cable_to_iface"],
        rear_port=parts["rear_port"],
        front_port=parts["front_port"],
    )
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_CABLE_NOT_TO_INTERFACE


def test_auth_mid_walk_clears_partial_results():
    circuit1 = _circuit(cid="CKT-1", circuit_id=100)
    circuit2 = _circuit(cid="CKT-2", circuit_id=101)
    device = _Record(id=1, name="R1", tag="border")
    iface = _Record(id=10, name="Eth1", device=device, device_id=1)

    ct1 = _Record(id=1, term_side="A", cable=_Record(id=50), circuit=circuit1, circuit_id=100)
    ct2 = _Record(id=2, term_side="A", cable=_Record(id=52), circuit=circuit2, circuit_id=101)
    cable1 = _Record(
        id=50,
        a_terminations=[{"object_type": "circuits.circuittermination", "object_id": ct1.id}],
        b_terminations=[{"object_type": "dcim.interface", "object_id": iface.id}],
    )
    cable2 = _Record(
        id=52,
        a_terminations=[{"object_type": "circuits.circuittermination", "object_id": ct2.id}],
        b_terminations=[{"object_type": "dcim.interface", "object_id": iface.id}],
    )
    terminations = [ct1, ct2]

    nb = _build_inventory_nb(
        circuit=circuit1,
        device=device,
        iface=iface,
        ct=ct1,
        cable=cable1,
        circuits=[circuit1, circuit2],
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

    def cable_get(pk):
        if pk == 50:
            return cable1
        if pk == 52:
            raise _netbox_request_error(403, "/api/dcim/cables/52/")
        return None

    nb.dcim.cables.get = cable_get

    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["stats"] == {"error": inv.ERROR_AUTH_DENIED}
    assert report["complete"] == []
    assert report["incomplete"] == []


def test_incomplete_provider_unavailable_when_circuit_provider_missing():
    provider = _provider(name="Cogent", provider_id=1)
    circuit = _scoped_circuit(provider_id=1)
    circuit.provider = 1
    nb = _build_inventory_nb(provider=provider, circuit=circuit, circuits=[circuit])

    class _ProvidersMissing:
        def all(self):
            return [provider]

        def filter(self, **kwargs):
            return [provider]

        def get(self, pk):
            return None

    nb.circuits.providers = _ProvidersMissing()
    report = inv.collect_uplink_inventory(nb, tag="border", circuit_scope=inv.project_circuit_scope())
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_PROVIDER_UNAVAILABLE
    assert report["incomplete"][0]["provider"] == "Cogent"


def test_incomplete_row_when_provider_circuits_query_fails():
    provider = _provider(name="Cogent", provider_id=1)

    class _CircuitsFail:
        def filter(self, **kwargs):
            raise RuntimeError("circuits unavailable")

        def get(self, pk):
            return None

    nb = _build_inventory_nb(provider=provider, circuits=[])
    nb.circuits.circuits = _CircuitsFail()
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["stats"]["read_errors"] == 1
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["provider"] == "Cogent"
    assert report["incomplete"][0]["reason"] == inv.REASON_PROVIDER_UNAVAILABLE


def _frn_lag_relations():
    return {
        "member_to_aggregate": {("FRN-MX-1", "et-0/0/3"): "ae5"},
        "parent_children": {("FRN-MX-1", "ae5"): {"ae5.0"}},
        "display_names": {
            ("FRN-MX-1", "et-0/0/3"): "et-0/0/3",
            ("FRN-MX-1", "ae5"): "ae5",
            ("FRN-MX-1", "ae5.0"): "ae5.0",
        },
    }


def test_pass_through_inactive_path_skipped():
    parts = _valid_odf_paths_result(None)
    paths_result = [
        {
            "id": 1,
            "is_active": False,
            "is_complete": True,
            "is_split": False,
            "origin": parts["border_iface"],
            "destination": parts["ct"],
            "path": [parts["segment"]],
        }
    ]
    nb, _rear_port = _build_odf_inventory_nb(
        paths_result=paths_result,
        border_iface=parts["border_iface"],
        ct=parts["ct"],
        cable_to_rear=parts["cable_to_rear"],
        cable_to_iface=parts["cable_to_iface"],
        rear_port=parts["rear_port"],
        front_port=parts["front_port"],
    )
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_CABLE_NOT_TO_INTERFACE


def test_pass_through_path_without_is_active_key_skipped():
    parts = _valid_odf_paths_result(None)
    paths_result = [
        {
            "id": 1,
            "is_complete": True,
            "is_split": False,
            "origin": parts["border_iface"],
            "destination": parts["ct"],
            "path": [parts["segment"]],
        }
    ]
    nb, _rear_port = _build_odf_inventory_nb(
        paths_result=paths_result,
        border_iface=parts["border_iface"],
        ct=parts["ct"],
        cable_to_rear=parts["cable_to_rear"],
        cable_to_iface=parts["cable_to_iface"],
        rear_port=parts["rear_port"],
        front_port=parts["front_port"],
    )
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_CABLE_NOT_TO_INTERFACE


def test_serialize_deserialize_netbox_interface_relations_roundtrip():
    relations = _frn_lag_relations()
    encoded = inv.serialize_netbox_interface_relations(relations)
    json.dumps(encoded)
    restored = inv.deserialize_netbox_interface_relations(encoded)
    assert restored == relations


def test_load_inventory_report_restores_netbox_relations(tmp_path):
    relations = _frn_lag_relations()
    payload = {
        "complete": [
            {
                "provider": "Hurricane",
                "device": "FRN-MX-1",
                "interface": "et-0/0/3",
                "circuit_id": "CKT-1",
            }
        ],
        "incomplete": [],
        "stats": {},
        "netbox_interface_relations": inv.serialize_netbox_interface_relations(relations),
    }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = inv.load_inventory_report(str(path))
    restored = inv.netbox_interface_relations_from_report(report)
    assert restored == relations
    mapping = inv.device_iface_provider_map_from_inventory(
        report, netbox_relations=restored
    )
    assert mapping[("FRN-MX-1", "ae5.0")] == "Hurricane"


def test_main_json_includes_serialized_netbox_relations(monkeypatch, netbox_env, capsys):
    nb = _build_inventory_nb(circuit=_scoped_circuit())
    with patch.object(inv.pynetbox, "api", lambda url, token: nb):
        monkeypatch.setattr(sys, "argv", ["netbox_uplinks_inventory.py", "--json"])
        assert inv.main() == 0
    payload = json.loads(capsys.readouterr().out)
    relations = payload.get("netbox_interface_relations")
    assert relations is not None
    assert isinstance(relations.get("member_to_aggregate"), list)
    assert isinstance(relations.get("parent_children"), list)
    assert isinstance(relations.get("display_names"), list)
    json.dumps(payload)


def test_main_json_nonempty_relations_excludes_internal_cache(monkeypatch, netbox_env, capsys):
    """Regression: tuple-key _netbox_interface_relations must not reach json.dumps."""
    nb = _build_inventory_nb(circuit=_scoped_circuit())
    relations = _frn_lag_relations()

    def collect_with_tuple_keys(nb_arg, device_names, debug=False, stats=None):
        assert nb_arg is nb
        return relations

    monkeypatch.setattr(inv, "collect_netbox_interface_relations", collect_with_tuple_keys)
    with patch.object(inv.pynetbox, "api", lambda url, token: nb):
        monkeypatch.setattr(sys, "argv", ["netbox_uplinks_inventory.py", "--json"])
        assert inv.main() == 0

    out = capsys.readouterr().out
    json.dumps(out)
    payload = json.loads(out)
    assert "_netbox_interface_relations" not in payload
    serialized = payload["netbox_interface_relations"]
    assert serialized["member_to_aggregate"]
    assert serialized["parent_children"]
    assert serialized["display_names"]
    json.dumps(payload)
    assert inv.deserialize_netbox_interface_relations(serialized) == relations


def test_load_uplink_provider_context_uses_embedded_relations(monkeypatch, netbox_env):
    relations = _frn_lag_relations()
    report = {
        "complete": [
            {
                "provider": "Hurricane",
                "device": "FRN-MX-1",
                "interface": "et-0/0/3",
                "circuit_id": "CKT-1",
            }
        ],
        "incomplete": [],
        "stats": {},
        "netbox_interface_relations": inv.serialize_netbox_interface_relations(relations),
    }
    inv.normalize_inventory_report(report)

    def fail_collect(*args, **kwargs):
        raise AssertionError("collect_netbox_interface_relations must not be called")

    def fail_netbox_client(*args, **kwargs):
        raise AssertionError("netbox_client_from_env must not be called when relations are embedded")

    monkeypatch.setattr(inv, "collect_netbox_interface_relations", fail_collect)
    monkeypatch.setattr(inv, "netbox_client_from_env", fail_netbox_client)
    ctx = inv.load_uplink_provider_context(inventory_report=report)
    assert ctx is not None
    assert ctx["netbox_interface_relations"] == relations
    assert ctx["device_iface_to_provider"][("FRN-MX-1", "ae5.0")] == "Hurricane"


def _frn_lag_relations():
    return {
        "member_to_aggregate": {("FRN-MX-1", "et-0/0/3"): "ae5"},
        "parent_children": {("FRN-MX-1", "ae5"): {"ae5.0"}},
        "display_names": {
            ("FRN-MX-1", "et-0/0/3"): "et-0/0/3",
            ("FRN-MX-1", "ae5"): "ae5",
            ("FRN-MX-1", "ae5.0"): "ae5.0",
        },
    }


def test_pass_through_inactive_path_skipped():
    parts = _valid_odf_paths_result(None)
    paths_result = [
        {
            "id": 1,
            "is_active": False,
            "is_complete": True,
            "is_split": False,
            "origin": parts["border_iface"],
            "destination": parts["ct"],
            "path": [parts["segment"]],
        }
    ]
    nb, _rear_port = _build_odf_inventory_nb(
        paths_result=paths_result,
        border_iface=parts["border_iface"],
        ct=parts["ct"],
        cable_to_rear=parts["cable_to_rear"],
        cable_to_iface=parts["cable_to_iface"],
        rear_port=parts["rear_port"],
        front_port=parts["front_port"],
    )
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_CABLE_NOT_TO_INTERFACE


def test_pass_through_path_without_is_active_key_skipped():
    parts = _valid_odf_paths_result(None)
    paths_result = [
        {
            "id": 1,
            "is_complete": True,
            "is_split": False,
            "origin": parts["border_iface"],
            "destination": parts["ct"],
            "path": [parts["segment"]],
        }
    ]
    nb, _rear_port = _build_odf_inventory_nb(
        paths_result=paths_result,
        border_iface=parts["border_iface"],
        ct=parts["ct"],
        cable_to_rear=parts["cable_to_rear"],
        cable_to_iface=parts["cable_to_iface"],
        rear_port=parts["rear_port"],
        front_port=parts["front_port"],
    )
    report = inv.collect_uplink_inventory(nb, tag="border")
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_CABLE_NOT_TO_INTERFACE


def test_serialize_deserialize_netbox_interface_relations_roundtrip():
    relations = _frn_lag_relations()
    encoded = inv.serialize_netbox_interface_relations(relations)
    json.dumps(encoded)
    restored = inv.deserialize_netbox_interface_relations(encoded)
    assert restored == relations


def test_load_inventory_report_restores_netbox_relations(tmp_path):
    relations = _frn_lag_relations()
    payload = {
        "complete": [
            {
                "provider": "Hurricane",
                "device": "FRN-MX-1",
                "interface": "et-0/0/3",
                "circuit_id": "CKT-1",
            }
        ],
        "incomplete": [],
        "stats": {},
        "netbox_interface_relations": inv.serialize_netbox_interface_relations(relations),
    }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = inv.load_inventory_report(str(path))
    restored = inv.netbox_interface_relations_from_report(report)
    assert restored == relations
    mapping = inv.device_iface_provider_map_from_inventory(
        report, netbox_relations=restored
    )
    assert mapping[("FRN-MX-1", "ae5.0")] == "Hurricane"


def test_main_json_includes_serialized_netbox_relations(monkeypatch, netbox_env, capsys):
    nb = _build_inventory_nb(circuit=_scoped_circuit())
    with patch.object(inv.pynetbox, "api", lambda url, token: nb):
        monkeypatch.setattr(sys, "argv", ["netbox_uplinks_inventory.py", "--json"])
        assert inv.main() == 0
    payload = json.loads(capsys.readouterr().out)
    relations = payload.get("netbox_interface_relations")
    assert relations is not None
    assert isinstance(relations.get("member_to_aggregate"), list)
    assert isinstance(relations.get("parent_children"), list)
    assert isinstance(relations.get("display_names"), list)
    json.dumps(payload)


def test_load_uplink_provider_context_uses_embedded_relations(monkeypatch, netbox_env):
    relations = _frn_lag_relations()
    report = {
        "complete": [
            {
                "provider": "Hurricane",
                "device": "FRN-MX-1",
                "interface": "et-0/0/3",
                "circuit_id": "CKT-1",
            }
        ],
        "incomplete": [],
        "stats": {},
        "netbox_interface_relations": inv.serialize_netbox_interface_relations(relations),
    }
    inv.normalize_inventory_report(report)

    def fail_collect(*args, **kwargs):
        raise AssertionError("collect_netbox_interface_relations must not be called")

    def fail_netbox_client(*args, **kwargs):
        raise AssertionError("netbox_client_from_env must not be called when relations are embedded")

    monkeypatch.setattr(inv, "collect_netbox_interface_relations", fail_collect)
    monkeypatch.setattr(inv, "netbox_client_from_env", fail_netbox_client)
    ctx = inv.load_uplink_provider_context(inventory_report=report)
    assert ctx is not None
    assert ctx["netbox_interface_relations"] == relations
    assert ctx["device_iface_to_provider"][("FRN-MX-1", "ae5.0")] == "Hurricane"
