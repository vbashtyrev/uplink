"""netbox_uplinks_cleanup: apply deletes, circuit types/providers, debug errors."""

from unittest.mock import MagicMock

import pytest

from netbox_uplinks_cleanup import (
    cleanup_cables,
    cleanup_circuit_terminations,
    cleanup_circuit_types,
    cleanup_circuits,
    cleanup_providers,
)
from tests.mocks.netbox_records import MutableNetBox, _Record


def test_cleanup_circuit_terminations_apply():
    nb = MutableNetBox()
    nb.circuits.circuit_terminations._items.append(_Record(id=1, circuit_id=5))
    n = cleanup_circuit_terminations(nb, [5], dry_run=False, debug=True)
    assert n == 1
    assert nb.circuits.circuit_terminations._items == []


def test_cleanup_circuit_terminations_delete_error_debug(capsys):
    nb = MagicMock()
    nb.circuits.circuit_terminations.filter.return_value = [_Record(id=3, circuit_id=1)]
    nb.circuits.circuit_terminations.delete.side_effect = RuntimeError("del fail")
    n = cleanup_circuit_terminations(nb, [1], dry_run=False, debug=True)
    assert n == 0
    assert "del fail" in capsys.readouterr().err


def test_cleanup_circuits_apply():
    nb = MutableNetBox()
    tag = _Record(id=1, slug="auto")
    nb.circuits.circuits._items.append(_Record(id=9, tags=[tag], tag_slug="auto"))
    n, _ = cleanup_circuits(nb, "auto", dry_run=False)
    assert n == 1
    assert nb.circuits.circuits._items == []


def test_cleanup_circuits_delete_error_debug(capsys):
    nb = MagicMock()
    nb.circuits.circuits.filter.return_value = [_Record(id=2, tags=[])]
    nb.circuits.circuits.delete.side_effect = RuntimeError("circuit del")
    n, _ = cleanup_circuits(nb, "auto", dry_run=False, debug=True)
    assert n == 0
    assert "circuit del" in capsys.readouterr().err


def test_cleanup_circuit_types_skip_when_in_use(capsys):
    nb = MutableNetBox()
    tag = _Record(id=1, slug="auto")
    ct = _Record(id=10, name="Transit", tags=[tag], tag_slug="auto")
    nb.circuits.circuit_types._items.append(ct)
    nb.circuits.circuits._items.append(_Record(id=1, type_id=10))
    n = cleanup_circuit_types(nb, "auto", dry_run=False, debug=True)
    assert n == 0
    assert "skip" in capsys.readouterr().err


def test_cleanup_circuit_types_apply_delete():
    nb = MutableNetBox()
    tag = _Record(id=1, slug="auto")
    nb.circuits.circuit_types._items.append(
        _Record(id=11, name="Unused", tags=[tag], tag_slug="auto")
    )
    n = cleanup_circuit_types(nb, "auto", dry_run=False)
    assert n == 1
    assert nb.circuits.circuit_types._items == []


def test_cleanup_circuit_types_dry_run():
    nb = MutableNetBox()
    tag = _Record(id=1, slug="auto")
    nb.circuits.circuit_types._items.append(
        _Record(id=12, name="T", tags=[tag], tag_slug="auto")
    )
    n = cleanup_circuit_types(nb, "auto", dry_run=True)
    assert n == 1
    assert len(nb.circuits.circuit_types._items) == 1


def test_cleanup_providers_apply_and_skip(capsys):
    nb = MutableNetBox()
    tag = _Record(id=1, slug="auto")
    p_free = _Record(id=20, name="FreeISP", tags=[tag], tag_slug="auto")
    p_busy = _Record(id=21, name="BusyISP", tags=[tag], tag_slug="auto")
    nb.circuits.providers._items.extend([p_free, p_busy])
    nb.circuits.circuits._items.append(_Record(id=1, provider_id=21))
    n = cleanup_providers(nb, "auto", dry_run=False, debug=True)
    assert n == 1
    assert len(nb.circuits.providers._items) == 1
    assert nb.circuits.providers._items[0].id == 21
    assert "skip" in capsys.readouterr().err


def test_cleanup_cables_apply():
    nb = MutableNetBox()
    tag = _Record(id=1, slug="auto")
    nb.dcim.cables._items.append(_Record(id=5, tags=[tag], tag_slug="auto"))
    n = cleanup_cables(nb, "auto", dry_run=False)
    assert n == 1
    assert not nb.dcim.cables._items


def test_cleanup_empty_tag_slug():
    nb = MutableNetBox()
    assert cleanup_cables(nb, "", dry_run=False) == 0
    assert cleanup_circuit_types(nb, None, dry_run=False) == 0
    assert cleanup_providers(nb, "", dry_run=False) == 0
    n, ids = cleanup_circuits(nb, "", dry_run=False)
    assert n == 0 and ids == []
