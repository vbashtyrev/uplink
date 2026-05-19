"""Mutable NetBox-like API for create/get/filter tests."""

from tests.mocks.netbox_api import _Record


def _record_has_tag(rec, tag_slug):
    tags = getattr(rec, "tags", None)
    if tags:
        for t in tags:
            slug = getattr(t, "slug", None) or (t if isinstance(t, str) else None)
            if slug == tag_slug:
                return True
    if getattr(rec, "tag", None) == tag_slug:
        return True
    if getattr(rec, "tag_slug", None) == tag_slug:
        return True
    return False


class _MutableEndpoint:
    def __init__(self, items=None):
        self._items = list(items or [])

    def filter(self, **kwargs):
        out = self._items
        for key, val in kwargs.items():
            if val is None:
                continue
            if key == "tag":
                out = [x for x in out if _record_has_tag(x, val)]
            elif key.endswith("_id"):
                base = key[:-3]
                out = [
                    x for x in out
                    if getattr(x, base, None) == val
                    or getattr(x, "{}_id".format(base), None) == val
                    or (hasattr(getattr(x, base, None), "id") and getattr(x, base).id == val)
                ]
            else:
                out = [x for x in out if getattr(x, key, None) == val]
        return out

    def delete(self, ids):
        id_set = {int(i) for i in (ids if isinstance(ids, (list, tuple)) else [ids])}
        self._items = [x for x in self._items if getattr(x, "id", None) not in id_set]

    def get(self, pk):
        for item in self._items:
            if getattr(item, "id", None) == pk:
                return item
        return None

    def create(self, **kwargs):
        rec = _Record(id=len(self._items) + 1, **kwargs)
        if "tags" in kwargs and kwargs["tags"]:
            rec.tags = [kwargs["tags"][0]] if not isinstance(kwargs["tags"][0], _Record) else kwargs["tags"]
        self._items.append(rec)
        return rec


class _TagsEndpoint:
    def __init__(self, tag=None):
        self._tag = tag

    def get(self, name=None, slug=None):
        return self._tag

    def create(self, name=None, slug=None):
        self._tag = _Record(id=1, name=name, slug=slug)
        return self._tag


class MutableNetBox:
    """NetBox API mock with in-memory providers, types, circuits, tags."""

    def __init__(self, automation_tag_name="automatization"):
        self.extras = type("Extras", (), {})()
        self.extras.tags = _TagsEndpoint()
        self.circuits = type("Circuits", (), {})()
        self.circuits.providers = _MutableEndpoint()
        self.circuits.circuit_types = _MutableEndpoint()
        self.circuits.circuits = _MutableEndpoint()
        self.circuits.circuit_terminations = _MutableEndpoint()
        self.dcim = type("Dcim", (), {})()
        self.dcim.devices = _MutableEndpoint()
        self.dcim.interfaces = _MutableEndpoint()
        self.dcim.cables = _MutableEndpoint()
        self.base_url = "https://netbox.example/api"
        self.token = "test-token"
        self._automation_tag_name = automation_tag_name

    def seed_automation_tag(self):
        tag = _Record(id=99, name=self._automation_tag_name, slug=self._automation_tag_name)
        self.extras.tags = _TagsEndpoint(tag=tag)
        return tag
