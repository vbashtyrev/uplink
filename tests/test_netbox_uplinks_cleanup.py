"""Tests for netbox_uplinks_cleanup.py with in-memory NetBox mock."""

import sys
from unittest.mock import patch

import pytest

from tests.mocks.netbox_records import MutableNetBox, _Record
from netbox_uplinks_cleanup import (
    cleanup_cables,
    cleanup_circuit_terminations,
    cleanup_circuit_types,
    cleanup_circuits,
    cleanup_providers,
    main,
)


def _tagged_nb():
    nb = MutableNetBox()
    tag = nb.seed_automation_tag()
    tag.slug = "automatization"
    return nb, tag


def test_cleanup_cables_dry_run():
    nb, tag = _tagged_nb()
    nb.dcim.cables._items.append(_Record(id=1, tags=[tag], tag_slug="automatization"))
    n = cleanup_cables(nb, "automatization", dry_run=True)
    assert n == 1
    assert len(nb.dcim.cables._items) == 1


def test_cleanup_cables_delete():
    nb, tag = _tagged_nb()
    nb.dcim.cables._items.append(_Record(id=1, tags=[tag]))
    n = cleanup_cables(nb, "automatization", dry_run=False)
    assert n == 1
    assert len(nb.dcim.cables._items) == 0


def test_cleanup_circuit_terminations():
    nb, _ = _tagged_nb()
    nb.circuits.circuit_terminations._items.append(_Record(id=5, circuit_id=10))
    n = cleanup_circuit_terminations(nb, [10], dry_run=False)
    assert n == 1


def test_cleanup_circuits_and_providers():
    nb, tag = _tagged_nb()
    nb.circuits.circuits._items.append(_Record(id=10, tags=[tag]))
    n, ids = cleanup_circuits(nb, "automatization", dry_run=False)
    assert n == 1
    assert ids == [10]

    nb.circuits.providers._items.append(_Record(id=2, tags=[tag], name="ISP"))
    n = cleanup_providers(nb, "automatization", dry_run=False)
    assert n == 1


def test_cleanup_circuit_types_skips_in_use():
    nb, tag = _tagged_nb()
    ct = _Record(id=3, tags=[tag], name="Internet")
    nb.circuits.circuit_types._items.append(ct)
    nb.circuits.circuits._items.append(_Record(id=99, type_id=3))
    n = cleanup_circuit_types(nb, "automatization", dry_run=True, debug=True)
    assert n == 0


def test_cleanup_circuit_types_deletes_unused():
    nb, tag = _tagged_nb()
    nb.circuits.circuit_types._items.append(_Record(id=3, tags=[tag], name="Internet"))
    n = cleanup_circuit_types(nb, "automatization", dry_run=False)
    assert n == 1


def test_main_dry_run(monkeypatch, netbox_env, capsys):
    nb, tag = _tagged_nb()
    nb.dcim.cables._items.append(_Record(id=1, tags=[tag]))
    nb.circuits.circuits._items.append(_Record(id=10, tags=[tag]))
    nb.circuits.circuit_terminations._items.append(_Record(id=5, circuit_id=10))

    with patch("netbox_uplinks_cleanup._get_nb", return_value=nb):
        with patch("netbox_uplinks_cleanup._get_automation_tag", return_value=tag):
            monkeypatch.setattr(sys, "argv", ["netbox_uplinks_cleanup.py", "--dry-run"])
            main()
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "cables" in out


def test_main_no_automation_tag(monkeypatch):
    import netbox_uplinks_cleanup as mod

    monkeypatch.setattr(mod, "AUTOMATION_TAG", "")
    monkeypatch.setattr(sys, "argv", ["netbox_uplinks_cleanup.py"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
