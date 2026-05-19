"""netbox_uplinks_cleanup cleanup_* functions."""

from unittest.mock import MagicMock

import netbox_uplinks_cleanup as nuc


def test_cleanup_circuits_dry_run_and_delete():
    nb = MagicMock()
    c1 = type("C", (), {"id": 1})()
    c2 = type("C", (), {"id": 2})()
    nb.circuits.circuits.filter.return_value = [c1, c2]
    n, ids = nuc.cleanup_circuits(nb, "automatization", dry_run=True, debug=True)
    assert n == 2
    assert ids == [1, 2]
    nb.circuits.circuits.delete.assert_not_called()
    n2, _ = nuc.cleanup_circuits(nb, "automatization", dry_run=False)
    assert n2 == 2


def test_cleanup_providers_skips_with_circuits(capsys):
    nb = MagicMock()
    prov = type("P", (), {"id": 5, "name": "Cogent"})()
    nb.circuits.providers.filter.return_value = [prov]
    nb.circuits.circuits.filter.return_value = [type("C", (), {"id": 1})()]
    assert nuc.cleanup_providers(nb, "automatization", dry_run=True, debug=True) == 0


def test_cleanup_circuit_types_deletes_unused():
    nb = MagicMock()
    ct = type("CT", (), {"id": 3, "name": "MPLS"})()
    nb.circuits.circuit_types.filter.return_value = [ct]
    nb.circuits.circuits.filter.return_value = []
    assert nuc.cleanup_circuit_types(nb, "automatization", dry_run=False) == 1
