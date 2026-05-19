"""create_termination when termination already has cable."""

from unittest.mock import MagicMock, patch

from netbox_create_circuits import create_termination_and_cable
from tests.mocks.netbox_full import NetBoxTestEnvironment


def test_existing_termination_with_cable_returns_early():
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    dev = env.dcim.devices._items[0]
    dev.site = 1
    iface = env.dcim.interfaces._items[0]
    prov, _ = env._ensure_provider("Cogent")
    ct_type = env.circuits.circuit_types._items[0]
    circuit = env.circuits.circuits.create(
        cid="WIRED", provider=prov.id, type=ct_type.id, commit_rate=1000
    )
    existing_ct = env.circuits.circuit_terminations.create(
        circuit=circuit.id,
        term_side="A",
        cable=MagicMock(id=88),
    )
    env.circuits.circuit_terminations.filter = lambda **k: [existing_ct]
    tagged_cable = MagicMock(id=88)
    env.dcim.cables.get = lambda pk: tagged_cable
    with patch("netbox_create_circuits._get_or_create_automation_tag", return_value=MagicMock(id=99)):
        with patch("netbox_create_circuits._ensure_record_tag") as ensure_tag:
            term, err = create_termination_and_cable(env, circuit, dev, iface)
    assert err is None
    assert term is existing_ct
    ensure_tag.assert_called()
