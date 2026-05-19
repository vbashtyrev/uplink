"""get_commit_rates_from_netbox: device_id lookup, circuit_id, object term attrs."""

from tests.mocks.netbox_api import MockNetBox, _Record
from zabbix_sync_commit_rate import KBPS_TO_BPS, get_commit_rates_from_netbox


def test_commit_rates_via_device_id_and_circuit_id():
    device = _Record(id=1, name="R1", tag="border")
    iface = _Record(id=10, name="Eth1", device_id=1)
    circuit = _Record(id=2, commit_rate=8000)
    ct = _Record(
        id=1,
        term_side="A",
        cable=_Record(id=50),
        circuit_id=2,
    )
    cable = _Record(
        id=50,
        a_terminations=[
            type("T", (), {"object_type": "circuits.circuittermination", "object_id": 1})(),
        ],
        b_terminations=[
            type("T", (), {"object_type": "dcim.interface", "object_id": 10})(),
        ],
    )
    nb = MockNetBox(
        devices=[device],
        interfaces=[iface],
        cables=[cable],
        terminations=[ct],
        circuits=[circuit],
    )
    result = get_commit_rates_from_netbox(nb, tag="border", debug=True)
    assert result == {("R1", "Eth1"): 8000 * KBPS_TO_BPS}
