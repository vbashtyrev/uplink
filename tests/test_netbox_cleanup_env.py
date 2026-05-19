"""netbox_uplinks_cleanup env error paths."""

import sys
from unittest.mock import patch

import pytest

from netbox_uplinks_cleanup import _get_automation_tag, _get_nb, cleanup_cables


def test_get_nb_missing_env(monkeypatch):
    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        _get_nb()


def test_get_automation_tag_missing():
    class Nb:
        class extras:
            class tags:
                @staticmethod
                def get(name=None, slug=None):
                    raise RuntimeError("api down")

    assert _get_automation_tag(Nb()) is None


def test_cleanup_cables_filter_error():
    class BadCables:
        def filter(self, **kwargs):
            raise RuntimeError("filter failed")

    class Nb:
        dcim = type("Dcim", (), {"cables": BadCables()})()

    assert cleanup_cables(Nb(), "tag", debug=True) == 0
