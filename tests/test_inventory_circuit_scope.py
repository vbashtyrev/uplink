"""Project circuit scope: monitor tag + uplinks_circuit_lifecycle=active."""

import sys
from unittest.mock import MagicMock, patch

from tests.mocks.netbox_api import MockNetBox, _Record, add_project_circuit_scope
from uplinks.netbox import inventory as inv
from uplinks_config import NETBOX_MONITOR_TAG


def _scoped_circuit(**overrides):
    base = {
        "id": 100,
        "cid": "CKT-1",
        "provider_id": 1,
        "provider": 1,
        "commit_rate": 10000000,
        "status": "active",
    }
    base.update(overrides)
    circuit = _Record(**base)
    return add_project_circuit_scope(circuit)


def _build_scoped_inventory_nb(provider=None, circuit=None, device=None, iface=None, ct=None, cable=None):
    provider = provider or _Record(id=1, name="Cogent")
    circuit = circuit or _scoped_circuit()
    circuit.provider_id = provider.id
    circuit.provider = provider.id
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
            return [provider]

        def filter(self, **kwargs):
            return [provider]

        def get(self, pk):
            return provider if provider.id == pk else None

    circuits = [circuit]

    class _CircuitsEndpoint:
        def filter(self, **kwargs):
            out = circuits
            if "provider_id" in kwargs:
                out = [c for c in out if c.provider_id == kwargs["provider_id"]]
            if "tag" in kwargs:
                out = [c for c in out if getattr(c, "tag", None) == kwargs["tag"]]
            return out

        def get(self, pk):
            for item in circuits:
                if item.id == pk:
                    return item
            return None

    nb = MockNetBox(
        devices=[device],
        interfaces=[iface],
        cables=[cable],
        terminations=[ct],
        circuits=circuits,
    )
    nb.circuits.providers = _Providers()
    nb.circuits.circuits = _CircuitsEndpoint()
    return nb


def test_collect_scope_includes_active_tagged_complete():
    nb = _build_scoped_inventory_nb()
    scope = inv.project_circuit_scope()
    report = inv.collect_uplink_inventory(nb, tag="border", circuit_scope=scope)
    assert len(report["complete"]) == 1
    assert report["complete"][0]["provider"] == "Cogent"
    assert report["incomplete"] == []


def test_collect_scope_reports_incomplete_for_active_tagged():
    provider = _Record(id=1, name="Cogent")
    circuit = _scoped_circuit()
    ct = _Record(id=1, term_side="A", cable=None, circuit=circuit, circuit_id=circuit.id)
    nb = _build_scoped_inventory_nb(provider=provider, circuit=circuit, ct=ct, cable=None)
    nb.dcim.cables = type("C", (), {"get": lambda self, pk: None})()
    scope = inv.project_circuit_scope()
    report = inv.collect_uplink_inventory(nb, tag="border", circuit_scope=scope)
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["reason"] == inv.REASON_NO_CABLE


def test_circuit_uplinks_tag_excluded_when_lifecycle_not_active():
    """Circuit tag uplinks without active lifecycle is out of project scope."""
    device = _Record(id=1, name="R1", tag="border")
    iface = _Record(id=10, name="Eth1", device=device, device_id=1)
    provider = _Record(id=1, name="TaggedISP")

    for lifecycle in ("review", "archived", None):
        custom_fields = (
            {"uplinks_circuit_lifecycle": lifecycle} if lifecycle is not None else {}
        )
        circuit = _Record(
            id=100,
            cid="CKT-1",
            provider_id=provider.id,
            provider=provider.id,
            commit_rate=10000000,
            status="active",
            tag=NETBOX_MONITOR_TAG,
            custom_fields=custom_fields,
        )
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
            b_terminations=[{"object_type": "dcim.interface", "object_id": iface.id}],
        )

        class _Providers:
            def all(self):
                return [provider]

            def filter(self, **kwargs):
                return [provider]

            def get(self, pk):
                return provider if provider.id == pk else None

        class _CircuitsEndpoint:
            def filter(self, **kwargs):
                return [circuit] if kwargs.get("provider_id") == provider.id else []

            def get(self, pk):
                return circuit if circuit.id == pk else None

        nb = MockNetBox(
            devices=[device],
            interfaces=[iface],
            cables=[cable],
            terminations=[ct],
            circuits=[circuit],
        )
        nb.circuits.providers = _Providers()
        nb.circuits.circuits = _CircuitsEndpoint()

        scope = inv.project_circuit_scope()
        report = inv.collect_uplink_inventory(nb, tag="border", circuit_scope=scope)
        assert report["complete"] == []
        assert report["incomplete"] == []


def test_one_provider_active_and_review_circuits_only_active_in_scope():
    """Single provider: active circuit in scope; review circuit omitted."""
    provider = _Record(id=1, name="Cogent")
    device = _Record(id=1, name="R1", tag="border")
    iface_active = _Record(id=10, name="Eth1", device=device, device_id=1)
    iface_review = _Record(id=11, name="Eth2", device=device, device_id=1)

    active_circuit = add_project_circuit_scope(
        _Record(
            id=100,
            cid="CKT-ACTIVE",
            provider_id=1,
            provider=1,
            commit_rate=1000,
            status="active",
        )
    )
    review_circuit = add_project_circuit_scope(
        _Record(
            id=101,
            cid="CKT-REVIEW",
            provider_id=1,
            provider=1,
            commit_rate=1000,
            status="active",
        ),
        lifecycle="review",
    )
    circuits = [active_circuit, review_circuit]

    ct_active = _Record(
        id=1,
        term_side="A",
        cable=_Record(id=50),
        circuit=active_circuit,
        circuit_id=active_circuit.id,
    )
    ct_review = _Record(
        id=2,
        term_side="A",
        cable=None,
        circuit=review_circuit,
        circuit_id=review_circuit.id,
    )
    cable = _Record(
        id=50,
        a_terminations=[{"object_type": "circuits.circuittermination", "object_id": ct_active.id}],
        b_terminations=[{"object_type": "dcim.interface", "object_id": iface_active.id}],
    )

    class _Providers:
        def all(self):
            return [provider]

        def filter(self, **kwargs):
            return [provider]

        def get(self, pk):
            return provider if provider.id == pk else None

    class _CircuitsEndpoint:
        def filter(self, **kwargs):
            if kwargs.get("provider_id") == 1:
                return circuits
            return []

        def get(self, pk):
            for circuit in circuits:
                if circuit.id == pk:
                    return circuit
            return None

    nb = MockNetBox(
        devices=[device],
        interfaces=[iface_active, iface_review],
        cables=[cable],
        terminations=[ct_active, ct_review],
        circuits=circuits,
    )
    nb.circuits.providers = _Providers()
    nb.circuits.circuits = _CircuitsEndpoint()

    scope = inv.project_circuit_scope()
    report = inv.collect_uplink_inventory(nb, tag="border", circuit_scope=scope)
    assert len(report["complete"]) == 1
    assert report["complete"][0]["circuit_id"] == "CKT-ACTIVE"
    assert report["incomplete"] == []


def test_collect_without_scope_audits_all_circuits():
    provider = _Record(id=1, name="LegacyISP")
    circuit = _Record(
        id=100,
        cid="CKT-1",
        provider_id=1,
        provider=1,
        commit_rate=1000,
        status="active",
    )
    ct = _Record(id=1, term_side="A", cable=None, circuit=circuit, circuit_id=circuit.id)
    nb = _build_scoped_inventory_nb(provider=provider, circuit=circuit, ct=ct, cable=None)
    nb.dcim.cables = type("C", (), {"get": lambda self, pk: None})()
    report = inv.collect_uplink_inventory(nb, tag="border", circuit_scope=None)
    assert report["complete"] == []
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["provider"] == "LegacyISP"


def test_main_ignores_out_of_scope_incomplete(monkeypatch, netbox_env, capsys):
    provider = _Record(id=1, name="ReviewISP")
    circuit = add_project_circuit_scope(
        _Record(
            id=100,
            cid="CKT-1",
            provider_id=provider.id,
            provider=provider.id,
            commit_rate=1000,
            status="active",
        ),
        lifecycle="review",
    )
    ct = _Record(id=1, term_side="A", cable=None, circuit=circuit, circuit_id=circuit.id)
    nb = _build_scoped_inventory_nb(provider=provider, circuit=circuit, ct=ct, cable=None)
    nb.dcim.cables = type("C", (), {"get": lambda self, pk: None})()
    with patch.object(inv.pynetbox, "api", lambda url, token: nb):
        monkeypatch.setattr(sys, "argv", ["netbox_uplinks_inventory.py", "--json"])
        assert inv.main() == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["complete"] == []
    assert payload["incomplete"] == []


def test_main_in_scope_incomplete_exit_code(monkeypatch, netbox_env, capsys):
    provider = _Record(id=1, name="Cogent")
    circuit = _scoped_circuit()
    ct = _Record(id=1, term_side="A", cable=None, circuit=circuit, circuit_id=circuit.id)
    nb = _build_scoped_inventory_nb(provider=provider, circuit=circuit, ct=ct, cable=None)
    nb.dcim.cables = type("C", (), {"get": lambda self, pk: None})()
    with patch.object(inv.pynetbox, "api", lambda url, token: nb):
        monkeypatch.setattr(sys, "argv", ["netbox_uplinks_inventory.py"])
        assert inv.main() == 1
    assert "INCOMPLETE" in capsys.readouterr().out


def test_providers_from_complete_inventory_scoped_active_tagged_only(monkeypatch):
    from tests.mocks.netbox_api import build_netbox_for_commit_rates

    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ActiveISP",
        device_tag="border",
        tag_device=True,
    )
    review_circuit = add_project_circuit_scope(
        _Record(
            id=200,
            cid="CKT-REVIEW",
            provider_id=nb.circuits.providers._providers[0].id,
            provider=nb.circuits.providers._providers[0].id,
            commit_rate=1000,
            status="active",
        ),
        lifecycle="review",
    )
    archived_circuit = add_project_circuit_scope(
        _Record(
            id=201,
            cid="CKT-ARCHIVED",
            provider_id=nb.circuits.providers._providers[0].id,
            provider=nb.circuits.providers._providers[0].id,
            commit_rate=1000,
            status="active",
        ),
        lifecycle="archived",
    )
    nb.circuits.circuits._items.extend([review_circuit, archived_circuit])

    report = inv.collect_uplink_inventory(nb, tag="border", circuit_scope=inv.project_circuit_scope())
    names = inv.providers_from_complete_inventory(report)

    assert names == {"ActiveISP"}


def test_physical_iface_maps_to_logical_for_zabbix():
    """Cable on member iface; Zabbix macros use logical ae5.0 via dry-ssh mapping."""
    dry_ssh = {
        "FRN-MX-1": [
            {"name": "ae5.0", "physicalInterface": "ae5", "isLogical": True},
            {"name": "ae5", "isLag": True},
            {"name": "et-0/0/3", "aggregateInterface": "ae5"},
        ],
    }
    report = {
        "complete": [
            {
                "provider": "Hurricane",
                "device": "FRN-MX-1",
                "interface": "et-0/0/3",
                "circuit_id": "CKT-1",
            },
        ],
    }
    mapping = inv.device_iface_provider_map_from_inventory(report, dry_ssh_devices=dry_ssh)
    assert mapping[("FRN-MX-1", "ae5.0")] == "Hurricane"
    assert mapping[("FRN-MX-1", "et-0/0/3")] == "Hurricane"


def test_collect_scope_is_read_only():
    nb = _build_scoped_inventory_nb()
    for endpoint_name in ("providers", "circuits", "circuit_terminations"):
        endpoint = getattr(nb.circuits, endpoint_name)
        endpoint.create = MagicMock(side_effect=AssertionError("create must not be called"))
        endpoint.delete = MagicMock(side_effect=AssertionError("delete must not be called"))
        if hasattr(endpoint, "update"):
            endpoint.update = MagicMock(side_effect=AssertionError("update must not be called"))
    scope = inv.project_circuit_scope()
    inv.collect_uplink_inventory(nb, tag="border", circuit_scope=scope, debug=True)


def test_circuit_lifecycle_comparison_ignores_case_and_spaces():
    scope = inv.project_circuit_scope()
    for lifecycle_value in (" Active ", "ACTIVE"):
        circuit = _Record(
            id=100,
            cid="CKT-1",
            status="active",
            tag=NETBOX_MONITOR_TAG,
            custom_fields={"uplinks_circuit_lifecycle": lifecycle_value},
        )
        assert inv.circuit_matches_scope(circuit, scope) is True
