"""Rich NetBox mock for create_circuits, cleanup, and checks integration tests."""

from tests.mocks.netbox_api import _Record
from tests.mocks.netbox_records import MutableNetBox, _MutableEndpoint


class NetBoxTestEnvironment(MutableNetBox):
    """NetBox API with devices, interfaces, tagged automation objects."""

    def __init__(self, automation_tag="automatization"):
        super().__init__(automation_tag_name=automation_tag)
        self.seed_automation_tag()
        self._tag_slug = automation_tag
        tag = self.extras.tags._tag
        tag.slug = automation_tag

    def add_device(self, name, device_id=None, tag=None):
        did = device_id or (len(self.dcim.devices._items) + 1)
        slug = tag or self._tag_slug
        dev = _Record(id=did, name=name, tag=slug, tag_slug=slug)
        self.dcim.devices._items.append(dev)
        return dev

    def add_interface(self, device, name, iface_id=None, speed=10000000, iface_type="virtual"):
        iid = iface_id or (len(self.dcim.interfaces._items) + 100)
        iface = _Record(
            id=iid,
            name=name,
            device=device,
            device_id=device.id,
            speed=speed,
            type={"value": iface_type, "label": iface_type},
            description="",
            mark_connected=False,
        )
        self.dcim.interfaces._items.append(iface)
        return iface

    def add_cable(self, cable_id, a_term, b_term):
        cable = _Record(id=cable_id, a_terminations=[a_term], b_terminations=[b_term])
        self.dcim.cables._items.append(cable)
        return cable

    def add_circuit_with_termination(self, cid, provider_name, commit_rate_kbps=10000000, iface=None):
        prov, _ = self._ensure_provider(provider_name)
        ct = self.circuits.circuit_types._items[0] if self.circuits.circuit_types._items else None
        if not ct:
            ct = self.circuits.circuit_types.create(name="Internet", slug="internet")
        circuit = _Record(
            id=len(self.circuits.circuits._items) + 1,
            cid=cid,
            provider=prov.id,
            provider_id=prov.id,
            type=ct.id,
            commit_rate=commit_rate_kbps,
        )
        self.circuits.circuits._items.append(circuit)
        term = _Record(
            id=len(self.circuits.circuit_terminations._items) + 1,
            circuit=circuit,
            circuit_id=circuit.id,
            term_side="A",
            cable=_Record(id=50) if iface else None,
        )
        self.circuits.circuit_terminations._items.append(term)
        if iface:
            self.add_cable(
                50,
                {"object_type": "circuits.circuittermination", "object_id": term.id},
                {"object_type": "dcim.interface", "object_id": iface.id},
            )
        return circuit, term

    def _ensure_provider(self, name):
        existing = list(self.circuits.providers.filter(name=name))
        if existing:
            return existing[0], None
        p = self.circuits.providers.create(name=name, slug=name.lower()[:50])
        return p, "created"

    def seed_for_create_circuits(self):
        """Typical env: one device with interface matching dry-ssh minimal."""
        dev = self.add_device("ALA-KZT-7280TR-1")
        self.add_interface(dev, "Ethernet51/1", speed=10000000, iface_type="10gbase-x-sfpp")
        if not self.circuits.circuit_types._items:
            self.circuits.circuit_types.create(name="Internet", slug="internet")
        return dev
