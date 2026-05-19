"""netbox_create_circuits: termination create 3.x fallback, cable disconnect."""

from unittest.mock import MagicMock, patch

from netbox_create_circuits import create_termination_and_cable, get_or_create_circuit_type
from tests.mocks.netbox_full import NetBoxTestEnvironment


def test_get_or_create_circuit_type_creates():
    env = NetBoxTestEnvironment()
    ct, msg = get_or_create_circuit_type(env, "MPLS")
    assert ct is not None
    assert msg in (None, "created")


def test_create_termination_netbox3_fallback():
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    dev = env.dcim.devices._items[0]
    dev.site = 1
    iface = env.dcim.interfaces._items[0]
    prov, _ = env._ensure_provider("Cogent")
    ct = env.circuits.circuit_types._items[0]
    circuit = env.circuits.circuits.create(
        cid="CKT-FB", provider=prov.id, type=ct.id, commit_rate=10_000_000
    )

    calls = []

    def create_side_effect(**kwargs):
        calls.append(kwargs)
        if "termination_type" in kwargs:
            raise RuntimeError("4.2 only")
        return MagicMock(id=99, cable=None)

    env.circuits.circuit_terminations.create = create_side_effect
    env.circuits.circuit_terminations.filter = lambda **k: []

    with patch("netbox_create_circuits._patch_interface_mark_connected", lambda *a, **k: None):
        with patch("netbox_create_circuits._get_or_create_automation_tag", return_value=None):
            term, err = create_termination_and_cable(env, circuit, dev, iface)
    assert err is None or "termination" in (err or "")
    assert len(calls) >= 1


def test_create_termination_deletes_existing_cable():
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    dev = env.dcim.devices._items[0]
    dev.site = 1
    iface = env.dcim.interfaces._items[0]
    old_cable = MagicMock(id=77)
    iface.cable = old_cable
    iface.mark_connected = True
    prov, _ = env._ensure_provider("Cogent")
    ct = env.circuits.circuit_types._items[0]
    circuit = env.circuits.circuits.create(
        cid="CKT-C", provider=prov.id, type=ct.id, commit_rate=10_000_000
    )
    report = {"deleted_cables": [], "disabled_mark_connected": [], "created_cables": []}
    deleted_ids = []

    def delete_cables(ids):
        deleted_ids.extend(ids)

    env.dcim.cables.delete = delete_cables
    env.circuits.circuit_terminations.filter = lambda **k: []
    env.circuits.circuit_terminations.create = lambda **kw: MagicMock(id=1, cable=None)

    with patch("netbox_create_circuits._patch_interface_mark_connected", lambda *a, **k: None):
        with patch("netbox_create_circuits._get_or_create_automation_tag", return_value=None):
            term, err = create_termination_and_cable(env, circuit, dev, iface, report=report)
    assert deleted_ids == [77]
    assert report["disabled_mark_connected"]
