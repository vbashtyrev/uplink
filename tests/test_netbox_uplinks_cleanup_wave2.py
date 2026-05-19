"""netbox_uplinks_cleanup: apply delete, debug paths, main without dry-run."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from netbox_uplinks_cleanup import (
    _get_automation_tag,
    _get_nb,
    cleanup_cables,
    cleanup_circuit_terminations,
    cleanup_circuits,
    main,
)
from tests.mocks.netbox_records import MutableNetBox, _Record


def test_cleanup_debug_filter_errors(capsys):
    nb = MagicMock()
    nb.dcim.cables.filter.side_effect = RuntimeError("filter fail")
    n = cleanup_cables(nb, "tag", dry_run=False, debug=True)
    assert n == 0
    assert "filter fail" in capsys.readouterr().err


def test_cleanup_circuit_terminations_dry_run():
    nb = MutableNetBox()
    nb.circuits.circuit_terminations._items.append(_Record(id=1, circuit_id=5))
    nb.circuits.circuit_terminations._items.append(_Record(id=2, circuit_id=5))
    n = cleanup_circuit_terminations(nb, [5], dry_run=True)
    assert n == 2


def test_cleanup_circuits_dry_run_returns_ids():
    nb = MutableNetBox()
    tag = _Record(id=1, slug="auto")
    nb.circuits.circuits._items.append(_Record(id=7, tags=[tag], tag_slug="auto"))
    n, ids = cleanup_circuits(nb, "auto", dry_run=True)
    assert n == 1
    assert ids == [7]


def test_main_apply_delete(monkeypatch, netbox_env, capsys):
    nb = MutableNetBox()
    tag = nb.seed_automation_tag()
    tag.slug = "automatization"
    nb.dcim.cables._items.append(_Record(id=1, tags=[tag], tag_slug="automatization"))
    nb.circuits.circuits._items.append(_Record(id=10, tags=[tag], tag_slug="automatization"))
    nb.circuits.circuit_terminations._items.append(_Record(id=5, circuit_id=10))

    with patch("netbox_uplinks_cleanup._get_nb", return_value=nb):
        with patch("netbox_uplinks_cleanup._get_automation_tag", return_value=tag):
            monkeypatch.setattr(sys, "argv", ["netbox_uplinks_cleanup.py"])
            main()
    out = capsys.readouterr().out
    assert "done:" in out
    assert len(nb.dcim.cables._items) == 0
    assert len(nb.circuits.circuits._items) == 0


def test_get_nb_missing_env(monkeypatch):
    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        _get_nb()


def test_get_automation_tag_not_found():
    nb = MutableNetBox()
    assert _get_automation_tag(nb) is None
