"""netbox_uplinks_cleanup main()."""

import sys
from unittest.mock import MagicMock, patch

import netbox_uplinks_cleanup as nuc


def test_main_cleanup_summary(monkeypatch, capsys):
    tag = MagicMock(slug="automatization")
    nb = MagicMock()
    with patch.object(nuc, "_get_nb", return_value=nb):
        with patch.object(nuc, "_get_automation_tag", return_value=tag):
            with patch.object(nuc, "cleanup_cables", return_value=2):
                with patch.object(nuc, "cleanup_circuit_terminations", return_value=1):
                    with patch.object(nuc, "cleanup_circuits", return_value=(3, [1, 2, 3])):
                        with patch.object(nuc, "cleanup_circuit_types", return_value=1):
                            with patch.object(nuc, "cleanup_providers", return_value=1):
                                nb.circuits.circuits.filter.return_value = []
                                monkeypatch.setattr(sys, "argv", ["netbox_uplinks_cleanup.py", "--dry-run"])
                                nuc.main()
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "cables" in out
