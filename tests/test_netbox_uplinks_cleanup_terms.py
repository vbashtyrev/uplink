"""netbox_uplinks_cleanup termination and tag helpers."""

from unittest.mock import MagicMock

import netbox_uplinks_cleanup as nuc


def test_cleanup_terminations_dry_run():
    nb = MagicMock()
    t1 = type("T", (), {"id": 1})()
    nb.circuits.circuit_terminations.filter.return_value = [t1]
    n = nuc.cleanup_circuit_terminations(nb, [10], dry_run=True, debug=True)
    assert n == 1
    nb.circuits.circuit_terminations.delete.assert_not_called()


def test_cleanup_terminations_delete():
    nb = MagicMock()
    nb.circuits.circuit_terminations.filter.return_value = [type("T", (), {"id": 2})()]
    assert nuc.cleanup_circuit_terminations(nb, [10], dry_run=False) == 1


def test_cleanup_cables_dry_run():
    nb = MagicMock()
    c = type("C", (), {"id": 5})()
    nb.dcim.cables.filter.return_value = [c]
    assert nuc.cleanup_cables(nb, "automatization", dry_run=True) == 1
