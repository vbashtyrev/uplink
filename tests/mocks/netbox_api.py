"""Minimal NetBox API mock for unit tests (pynetbox-like records)."""


class _Record:
    def __init__(self, **fields):
        for key, val in fields.items():
            setattr(self, key, val)

    def update(self, data):
        for key, val in (data or {}).items():
            setattr(self, key, val)


class _Filterable:
    def __init__(self, items):
        self._items = list(items)

    def filter(self, **kwargs):
        out = self._items
        for key, val in kwargs.items():
            if val is None:
                continue
            out = [x for x in out if getattr(x, key, None) == val]
        return out

    def get(self, pk):
        for item in self._items:
            if getattr(item, "id", None) == pk or str(getattr(item, "id", "")) == str(pk):
                return item
        return None


class _Dcim:
    def __init__(self, devices, interfaces, cables):
        self.devices = _Filterable(devices)
        self.interfaces = _Filterable(interfaces)
        self.cables = _Filterable(cables)


class _Circuits:
    def __init__(self, terminations, circuits):
        self.circuit_terminations = _Filterable(terminations)
        self.circuits = _Filterable(circuits)


class MockNetBox:
    """Thin stand-in for pynetbox.api() return value."""

    def __init__(self, devices, interfaces, cables, terminations, circuits):
        self.dcim = _Dcim(devices, interfaces, cables)
        self.circuits = _Circuits(terminations, circuits)


def build_netbox_for_commit_rates(
    *,
    device_name="router1",
    device_id=1,
    iface_name="Ethernet51/1",
    iface_id=10,
    commit_rate_kbps=10000,
    tag_device=True,
    device_tag="uplinks",
    cable_id=50,
    ct_id=1,
    circuit_id=100,
):
    """
    Build NetBox mock wired for zabbix_sync_commit_rate.get_commit_rates_from_netbox:
    circuit termination (A) -> cable -> interface on tagged device -> circuit commit_rate.
    """
    device = _Record(id=device_id, name=device_name, tag=device_tag if tag_device else None)
    iface = _Record(id=iface_id, name=iface_name, device=device, device_id=device_id)
    circuit = _Record(id=circuit_id, commit_rate=commit_rate_kbps)
    ct = _Record(
        id=ct_id,
        term_side="A",
        cable=_Record(id=cable_id),
        circuit=circuit,
        circuit_id=circuit_id,
    )
    cable = _Record(
        id=cable_id,
        a_terminations=[
            {"object_type": "circuits.circuittermination", "object_id": ct_id},
        ],
        b_terminations=[
            {"object_type": "dcim.interface", "object_id": iface_id},
        ],
    )
    devices = [device] if tag_device else []
    return MockNetBox(
        devices=devices,
        interfaces=[iface],
        cables=[cable],
        terminations=[ct],
        circuits=[circuit],
    )
