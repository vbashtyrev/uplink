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


class _Providers:
    def __init__(self, providers):
        self._providers = list(providers)

    def all(self):
        return self._providers

    def filter(self, **kwargs):
        return self._providers

    def get(self, pk):
        for provider in self._providers:
            if provider.id == pk:
                return provider
        return None


class _CircuitsEndpoint:
    def __init__(self, items):
        self._items = list(items)

    def filter(self, **kwargs):
        out = self._items
        if "provider_id" in kwargs:
            out = [c for c in out if getattr(c, "provider_id", None) == kwargs["provider_id"]]
        return out

    def get(self, pk):
        for circuit in self._items:
            if circuit.id == pk:
                return circuit
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


def wire_inventory_collector(nb, provider=None, circuits=None):
    """
    Attach providers/circuits endpoints required by collect_uplink_inventory / get_commit_rates_from_netbox.
    Mutates nb in place; returns nb.
    """
    if circuits is None:
        circuits = list(nb.circuits.circuits._items)
    if provider is None:
        provider = _Record(id=1, name="TestProvider")
    for circuit in circuits:
        if getattr(circuit, "provider_id", None) is None:
            circuit.provider_id = provider.id
        if getattr(circuit, "provider", None) is None:
            circuit.provider = provider.id
        if getattr(circuit, "status", None) is None:
            circuit.status = "active"
        if getattr(circuit, "cid", None) is None and getattr(circuit, "id", None) is not None:
            circuit.cid = "CKT-{}".format(circuit.id)
    nb.circuits.providers = _Providers([provider])
    nb.circuits.circuits = _CircuitsEndpoint(circuits)
    return nb


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
    provider_id=1,
    provider_name="TestProvider",
    circuit_status="active",
):
    """
    Build NetBox mock wired for zabbix_sync_commit_rate.get_commit_rates_from_netbox:
    provider -> active circuit -> termination (A) -> cable -> interface on tagged device.
    """
    provider = _Record(id=provider_id, name=provider_name)
    device = _Record(id=device_id, name=device_name, tag=device_tag if tag_device else None)
    iface = _Record(id=iface_id, name=iface_name, device=device, device_id=device_id)
    circuit = _Record(
        id=circuit_id,
        cid="CKT-{}".format(circuit_id),
        provider_id=provider_id,
        provider=provider_id,
        commit_rate=commit_rate_kbps,
        status=circuit_status,
    )
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
    devices = [device] if tag_device else [device]
    nb = MockNetBox(
        devices=devices,
        interfaces=[iface],
        cables=[cable],
        terminations=[ct],
        circuits=[circuit],
    )
    wire_inventory_collector(nb, provider=provider, circuits=[circuit])
    return nb
