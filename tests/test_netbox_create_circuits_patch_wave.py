"""netbox_create_circuits: REST patch, tags, resolve_physical."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from netbox_create_circuits import (
    _ensure_record_tag,
    _get_or_create_automation_tag,
    _patch_interface_mark_connected,
    resolve_physical_interface,
)
from tests.mocks.netbox_records import MutableNetBox, _Record


def test_patch_interface_mark_connected(monkeypatch):
    nb = MagicMock()
    nb.base_url = "https://nb.example/api"
    nb.token = "tok"
    calls = []
    resp = MagicMock()
    resp.raise_for_status = MagicMock()

    def fake_patch(url, **kwargs):
        calls.append((url, kwargs))
        return resp

    monkeypatch.setattr(requests, "patch", fake_patch)
    _patch_interface_mark_connected(nb, 42, False)
    assert "/dcim/interfaces/42/" in calls[0][0]
    assert calls[0][1]["json"] == {"mark_connected": False}


def test_patch_interface_no_credentials(monkeypatch):
    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    nb = MagicMock()
    nb.base_url = ""
    nb.token = ""
    with pytest.raises(RuntimeError, match="base_url"):
        _patch_interface_mark_connected(nb, 1, True)


def test_get_or_create_automation_tag_creates():
    nb = MutableNetBox()

    class Tags:
        def get(self, name=None, slug=None):
            return None

        def create(self, name=None, slug=None):
            return _Record(id=2, name=name, slug=slug)

    nb.extras.tags = Tags()
    tag = _get_or_create_automation_tag(nb)
    assert tag is not None
    assert tag.slug == "automatization"


def test_ensure_record_tag_adds_tag():
    nb = MutableNetBox()
    tag = _Record(id=5, slug="auto")
    rec = _Record(id=10, tags=[])
    nb.circuits.providers._items.append(rec)

    class EP:
        def get(self, pk):
            return rec

        def update(self, data):
            rec.tags = [_Record(id=i) for i in data.get("tags", [])]

    _ensure_record_tag(nb, rec, tag, EP())
    assert 5 in rec.tags


def test_resolve_physical_interface_logical():
    devices = {
        "R1": [
            {
                "name": "ae5.0",
                "isLogical": True,
                "physicalInterface": "et-0/0/1",
            },
        ],
    }
    assert resolve_physical_interface("R1", "ae5.0", devices) == "et-0/0/1"
    assert resolve_physical_interface("R1", "ge-0/0/0", devices) == "ge-0/0/0"
