"""Tests for netbox_interface_types.py (mocked GitHub fetch)."""

import json
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _choices_py_text():
    return (FIXTURES / "netbox_choices_snippet.py").read_text(encoding="utf-8")


def test_fetch_interface_types_from_github(monkeypatch):
    import netbox_interface_types as mod

    class FakeResp:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        "requests.get",
        lambda url, timeout=15: FakeResp(_choices_py_text()),
    )
    types = mod._fetch_interface_types_from_github()
    values = {t["value"] for t in types}
    assert "10gbase-x-sfpp" in values
    assert "virtual" in values
    labels = {t["label"] for t in types}
    assert "SFP+ (10GE)" in labels


def test_fetch_interface_types_download_error(monkeypatch, capsys):
    import netbox_interface_types as mod

    def fail_get(url, timeout=15):
        raise OSError("network down")

    monkeypatch.setattr("requests.get", fail_get)
    assert mod._fetch_interface_types_from_github() == []
    assert "Error downloading" in capsys.readouterr().err


def test_fetch_interface_types_no_class(monkeypatch, capsys):
    import netbox_interface_types as mod

    class FakeResp:
        text = "# no InterfaceTypeChoices here\n"
        def raise_for_status(self):
            pass

    monkeypatch.setattr("requests.get", lambda url, timeout=15: FakeResp())
    assert mod._fetch_interface_types_from_github() == []
    assert "not found" in capsys.readouterr().err


def test_main_writes_json(monkeypatch, tmp_path):
    import netbox_interface_types as mod

    monkeypatch.setattr(
        mod,
        "_fetch_interface_types_from_github",
        lambda: [{"value": "virtual", "label": "Virtual"}],
    )
    out = tmp_path / "types.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["netbox_interface_types.py", "-o", str(out)],
    )
    assert mod.main() == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["interface_types"][0]["value"] == "virtual"


def test_main_empty_types_exits(monkeypatch):
    import netbox_interface_types as mod

    monkeypatch.setattr(mod, "_fetch_interface_types_from_github", lambda: [])
    monkeypatch.setattr(sys, "argv", ["netbox_interface_types.py"])
    assert mod.main() == 1
