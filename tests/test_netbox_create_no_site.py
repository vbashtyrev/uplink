"""create_termination_and_cable when device has no site."""

from netbox_create_circuits import create_termination_and_cable
from tests.mocks.netbox_full import NetBoxTestEnvironment


def test_create_termination_no_site():
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    dev = env.dcim.devices._items[0]
    dev.site = None
    iface = env.dcim.interfaces._items[0]
    prov, _ = env._ensure_provider("Cogent")
    ct = env.circuits.circuit_types._items[0]
    circuit = env.circuits.circuits.create(
        cid="NO-SITE", provider=prov.id, type=ct.id, commit_rate=1000
    )
    term, err = create_termination_and_cable(env, circuit, dev, iface)
    assert term is None
    assert "site" in (err or "").lower()
