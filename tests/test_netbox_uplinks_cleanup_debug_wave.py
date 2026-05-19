"""netbox_uplinks_cleanup: filter errors and circuit_ids in main."""

import sys
from unittest.mock import MagicMock, patch

import netbox_uplinks_cleanup as nuc
from netbox_uplinks_cleanup import cleanup_circuit_types, cleanup_providers
from tests.mocks.netbox_records import MutableNetBox, _Record


def test_cleanup_circuit_types_filter_error(capsys):
    nb = MagicMock()
    nb.circuits.circuit_types.filter.side_effect = RuntimeError("ct filter")
    assert cleanup_circuit_types(nb, "auto", debug=True) == 0
    assert "ct filter" in capsys.readouterr().err


def test_cleanup_providers_filter_error(capsys):
    nb = MagicMock()
    nb.circuits.providers.filter.side_effect = RuntimeError("prov filter")
    assert cleanup_providers(nb, "auto", debug=True) == 0
    assert "prov filter" in capsys.readouterr().err


def test_main_collects_circuit_ids(monkeypatch, capsys):
    tag = MagicMock(slug="automatization")
    nb = MagicMock()
    nb.circuits.circuits.filter.return_value = [_Record(id=7)]
    with patch.object(nuc, "_get_nb", return_value=nb):
        with patch.object(nuc, "_get_automation_tag", return_value=tag):
            with patch.object(nuc, "cleanup_cables", return_value=0):
                with patch.object(nuc, "cleanup_circuit_terminations", return_value=0) as terms:
                    with patch.object(nuc, "cleanup_circuits", return_value=(0, [])):
                        with patch.object(nuc, "cleanup_circuit_types", return_value=0):
                            with patch.object(nuc, "cleanup_providers", return_value=0):
                                monkeypatch.setattr(
                                    sys, "argv", ["netbox_uplinks_cleanup.py", "--dry-run"]
                                )
                                nuc.main()
    terms.assert_called_once_with(nb, [7], dry_run=True, debug=False)
