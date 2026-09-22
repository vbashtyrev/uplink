"""Project circuit scope: Circuit type Uplink + built-in status Active."""

import sys
from unittest.mock import MagicMock, patch

from tests.mocks.netbox_api import MockNetBox, _Record, add_project_circuit_scope
from uplinks.netbox import inventory as inv


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


def test_collect_scope_includes_active_uplink_type_complete():
    nb = _build_scoped_inventory_nb()
    scope = inv.project_circuit_scope()
    report = inv.collect_uplink_inventory(nb, tag="border", circuit_scope=scope)
    assert len(report["complete"]) == 1
    assert report["complete"][0]["provider"] == "Cogent"
    assert report["incomplete"] == []


def test_collect_scope_reports_incomplete_for_active_uplink_type():
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


def test_circuit_wrong_type_excluded_from_scope():
    device = _Record(id=1, name="R1", tag="border")
    iface = _Record(id=10, name="Eth1", device=device, device_id=1)
    provider = _Record(id=1, name="OtherISP")

    for type_name, type_slug in (("Internet", "internet"), ("Transit", "transit"), (None, None)):
        if type_name is None:
            circuit = _Record(
                id=100,
                cid="CKT-1",
                provider_id=provider.id,
                provider=provider.id,
                commit_rate=10000000,
                status="active",
            )
        else:
            circuit = _Record(
                id=100,
                cid="CKT-1",
                provider_id=provider.id,
                provider=provider.id,
                commit_rate=10000000,
                status="active",
                type=_Record(name=type_name, slug=type_slug),
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


def test_circuit_inactive_status_excluded_from_active_only():
    provider = _Record(id=1, name="Cogent")
    circuit = add_project_circuit_scope(
        _Record(
            id=100,
            cid="CKT-PLANNED",
            provider_id=1,
            provider=1,
            commit_rate=1000,
            status="planned",
        )
    )
    nb = _build_scoped_inventory_nb(provider=provider, circuit=circuit)
    scope = inv.project_circuit_scope()
    report = inv.collect_uplink_inventory(nb, tag="border", circuit_scope=scope, active_only=True)
    assert report["complete"] == []
    assert report["incomplete"] == []


def test_circuit_uplink_type_name_normalized():
    circuit = _Record(
        id=100,
        cid="CKT-1",
        status="active",
        type=_Record(name=" Uplink ", slug="uplink"),
    )
    scope = inv.project_circuit_scope()
    assert inv.circuit_matches_scope(circuit, scope) is True

    circuit.type = _Record(name="UPLINK", slug="uplink")
    assert inv.circuit_matches_scope(circuit, scope) is True


def test_one_provider_active_and_non_uplink_only_uplink_in_scope():
    provider = _Record(id=1, name="Cogent")
    device = _Record(id=1, name="R1", tag="border")
    iface_active = _Record(id=10, name="Eth1", device=device, device_id=1)
    iface_other = _Record(id=11, name="Eth2", device=device, device_id=1)

    uplink_circuit = add_project_circuit_scope(
        _Record(
            id=100,
            cid="CKT-UPLINK",
            provider_id=1,
            provider=1,
            commit_rate=1000,
            status="active",
        )
    )
    other_circuit = _Record(
        id=101,
        cid="CKT-INTERNET",
        provider_id=1,
        provider=1,
        commit_rate=1000,
        status="active",
        type=_Record(name="Internet", slug="internet"),
    )
    circuits = [uplink_circuit, other_circuit]

    ct_active = _Record(
        id=1,
        term_side="A",
        cable=_Record(id=50),
        circuit=uplink_circuit,
        circuit_id=uplink_circuit.id,
    )
    ct_other = _Record(
        id=2,
        term_side="A",
        cable=None,
        circuit=other_circuit,
        circuit_id=other_circuit.id,
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
        interfaces=[iface_active, iface_other],
        cables=[cable],
        terminations=[ct_active, ct_other],
        circuits=circuits,
    )
    nb.circuits.providers = _Providers()
    nb.circuits.circuits = _CircuitsEndpoint()

    scope = inv.project_circuit_scope()
    report = inv.collect_uplink_inventory(nb, tag="border", circuit_scope=scope)
    assert len(report["complete"]) == 1
    assert report["complete"][0]["circuit_id"] == "CKT-UPLINK"
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
    provider = _Record(id=1, name="InternetISP")
    circuit = _Record(
        id=100,
        cid="CKT-1",
        provider_id=provider.id,
        provider=provider.id,
        commit_rate=1000,
        status="active",
        type=_Record(name="Internet", slug="internet"),
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


def test_providers_from_complete_inventory_scoped_uplink_only(monkeypatch):
    from tests.mocks.netbox_api import build_netbox_for_commit_rates

    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ActiveISP",
        device_tag="border",
        tag_device=True,
    )
    other_circuit = _Record(
        id=200,
        cid="CKT-INTERNET",
        provider_id=nb.circuits.providers._providers[0].id,
        provider=nb.circuits.providers._providers[0].id,
        commit_rate=1000,
        status="active",
        type=_Record(name="Internet", slug="internet"),
    )
    nb.circuits.circuits._items.append(other_circuit)

    report = inv.collect_uplink_inventory(nb, tag="border", circuit_scope=inv.project_circuit_scope())
    names = inv.providers_from_complete_inventory(report)

    assert names == {"ActiveISP"}
    assert report["stats"]["providers_in_scope"] == 1


def test_physical_iface_maps_to_logical_via_netbox_relations():
    """Cable on LAG member; provider map expands to logical unit via NetBox lag/parent."""
    lag = _Record(id=20, name="ae5")
    logical = _Record(id=21, name="ae5.0", parent=lag)
    member = _Record(id=22, name="et-0/0/3", lag=lag)
    relations = {
        "member_to_aggregate": {("FRN-MX-1", "et-0/0/3"): "ae5"},
        "parent_children": {("FRN-MX-1", "ae5"): {"ae5.0"}},
        "display_names": {
            ("FRN-MX-1", "et-0/0/3"): "et-0/0/3",
            ("FRN-MX-1", "ae5"): "ae5",
            ("FRN-MX-1", "ae5.0"): "ae5.0",
        },
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
    mapping = inv.device_iface_provider_map_from_inventory(
        report, netbox_relations=relations
    )
    assert mapping[("FRN-MX-1", "ae5.0")] == "Hurricane"
    assert mapping[("FRN-MX-1", "et-0/0/3")] == "Hurricane"


def test_netbox_relations_skips_nonzero_units_when_unit0_missing():
    """LAG children ae5.1/ae5.32767 without ae5.0 must not map to dotted units."""
    relations = {
        "member_to_aggregate": {},
        "parent_children": {
            ("FRN-MX-1", "ae5"): {"ae5.1", "ae5.32767"},
        },
        "display_names": {
            ("FRN-MX-1", "ae5"): "ae5",
            ("FRN-MX-1", "ae5.1"): "ae5.1",
            ("FRN-MX-1", "ae5.32767"): "ae5.32767",
        },
    }
    report = {
        "complete": [
            {
                "provider": "Hurricane",
                "device": "FRN-MX-1",
                "interface": "ae5",
                "circuit_id": "CKT-1",
            },
        ],
    }
    mapping = inv.device_iface_provider_map_from_inventory(
        report, netbox_relations=relations
    )
    assert mapping[("FRN-MX-1", "ae5")] == "Hurricane"
    assert ("FRN-MX-1", "ae5.0") not in mapping
    assert ("FRN-MX-1", "ae5.1") not in mapping
    assert ("FRN-MX-1", "ae5.32767") not in mapping

    burst_report = {
        "complete": [
            {
                "provider": "Hurricane",
                "device": "FRN-MX-1",
                "interface": "ae5",
                "circuit_id": "CKT-1",
                "billing_model": "Burst",
            },
        ],
    }
    pairs = inv.burst_pairs_from_inventory(burst_report)
    expanded = inv.expand_burst_pairs_for_zabbix(pairs, netbox_relations=relations)
    assert expanded == {("FRN-MX-1", "ae5")}


def test_netbox_relations_picks_single_logical_unit_not_all_children():
    """LAG with ae5.0/ae5.1/ae5.32767 expands only preferred Zabbix unit (.0)."""
    relations = {
        "member_to_aggregate": {},
        "parent_children": {
            ("FRN-MX-1", "ae5"): {"ae5.0", "ae5.1", "ae5.32767"},
        },
        "display_names": {
            ("FRN-MX-1", "ae5"): "ae5",
            ("FRN-MX-1", "ae5.0"): "ae5.0",
            ("FRN-MX-1", "ae5.1"): "ae5.1",
            ("FRN-MX-1", "ae5.32767"): "ae5.32767",
        },
    }
    report = {
        "complete": [
            {
                "provider": "Hurricane",
                "device": "FRN-MX-1",
                "interface": "ae5",
                "circuit_id": "CKT-1",
            },
        ],
    }
    mapping = inv.device_iface_provider_map_from_inventory(
        report, netbox_relations=relations
    )
    assert mapping[("FRN-MX-1", "ae5")] == "Hurricane"
    assert mapping[("FRN-MX-1", "ae5.0")] == "Hurricane"
    assert ("FRN-MX-1", "ae5.1") not in mapping
    assert ("FRN-MX-1", "ae5.32767") not in mapping


def test_expand_without_netbox_relations_fails_closed_no_logical_alias():
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
    mapping = inv.device_iface_provider_map_from_inventory(report)
    assert ("FRN-MX-1", "ae5.0") not in mapping
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
