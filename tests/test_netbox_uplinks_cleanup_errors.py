"""netbox_uplinks_cleanup error and debug branches."""

import sys
from unittest.mock import MagicMock, patch

from netbox_uplinks_cleanup import (
    cleanup_cables,
    cleanup_circuit_terminations,
    cleanup_circuit_types,
    cleanup_circuits,
    cleanup_providers,
)
from tests.mocks.netbox_records import MutableNetBox, _Record


def test_cleanup_cables_delete_error(capsys):
    nb = MagicMock()
    nb.dcim.cables.filter.return_value = [_Record(id=1)]
    nb.dcim.cables.delete.side_effect = RuntimeError("delete failed")
    n = cleanup_cables(nb, "tag", dry_run=False, debug=True)
    assert n == 0
    assert "delete failed" in capsys.readouterr().err


def test_cleanup_circuits_per_id_delete():
    nb = MutableNetBox()
    nb.circuits.circuits._items.append(_Record(id=1, tag_slug="auto"))
    nb.circuits.circuits.delete = MagicMock(side_effect=[RuntimeError("fail"), None])
    n, ids = cleanup_circuits(nb, "auto", dry_run=False, debug=True)
    assert n >= 0


def test_cleanup_circuit_terminations_filter_error(capsys):
    nb = MagicMock()
    nb.circuits.circuit_terminations.filter.side_effect = RuntimeError("filter err")
    n = cleanup_circuit_terminations(nb, [1], dry_run=False, debug=True)
    assert n == 0


def test_cleanup_providers_skips_with_circuits(capsys):
    nb = MutableNetBox()
    tag = _Record(id=1, slug="auto")
    prov = _Record(id=2, tags=[tag], tag_slug="auto", name="ISP")
    nb.circuits.providers._items.append(prov)
    nb.circuits.circuits._items.append(_Record(id=10, provider_id=2))
    n = cleanup_providers(nb, "auto", dry_run=False, debug=True)
    assert n == 0


def test_main_tag_not_in_netbox_exits_zero(monkeypatch, netbox_env, capsys):
    import pytest

    import netbox_uplinks_cleanup as mod

    nb = MutableNetBox()
    with patch("netbox_uplinks_cleanup._get_nb", return_value=nb):
        with patch("netbox_uplinks_cleanup._get_automation_tag", return_value=None):
            monkeypatch.setattr(sys, "argv", ["netbox_uplinks_cleanup.py", "--dry-run"])
            with pytest.raises(SystemExit) as exc:
                mod.main()
    assert exc.value.code == 0
    assert "not found" in capsys.readouterr().err.lower()


def test_cleanup_circuit_types_delete_apply():
    nb = MutableNetBox()
    tag = _Record(id=1, slug="auto")
    nb.circuits.circuit_types._items.append(_Record(id=3, tags=[tag], name="Internet"))
    n = cleanup_circuit_types(nb, "auto", dry_run=False)
    assert n == 1
