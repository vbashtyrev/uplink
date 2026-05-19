"""get_commit_rates_from_netbox skip paths (no interface on cable, untagged device)."""

from tests.mocks.netbox_api import MockNetBox, _Record
from zabbix_sync_commit_rate import get_commit_rates_from_netbox


def test_skips_termination_without_interface_on_cable():
    ct = _Record(id=1, term_side="A", cable=_Record(id=50), circuit=_Record(id=2, commit_rate=1000))
    cable = _Record(
        id=50,
        a_terminations=[{"object_type": "circuits.circuittermination", "object_id": 1}],
        b_terminations=[{"object_type": "circuits.circuittermination", "object_id": 2}],
    )
    nb = MockNetBox(devices=[], interfaces=[], cables=[cable], terminations=[ct], circuits=[ct.circuit])
    assert get_commit_rates_from_netbox(nb, tag=None, debug=True) == {}


def test_skips_untagged_when_tag_filter():
    device = _Record(id=1, name="R1", tag="other")
    iface = _Record(id=10, name="Eth1", device=device, device_id=1)
    circuit = _Record(id=2, commit_rate=5000)
    ct = _Record(id=1, term_side="A", cable=_Record(id=50), circuit=circuit, circuit_id=2)
    cable = _Record(
        id=50,
        a_terminations=[{"object_type": "circuits.circuittermination", "object_id": 1}],
        b_terminations=[{"object_type": "dcim.interface", "object_id": 10}],
    )
    nb = MockNetBox(devices=[device], interfaces=[iface], cables=[cable], terminations=[ct], circuits=[circuit])
    assert get_commit_rates_from_netbox(nb, tag="border", debug=False) == {}
