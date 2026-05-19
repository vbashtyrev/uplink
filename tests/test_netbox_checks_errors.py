"""netbox_checks error paths: missing file, host, netbox."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import netbox_checks as nc

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_missing_file(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["netbox_checks.py", "-f", "/no/such/file.json"])
    assert nc.main() == 1


def test_main_host_not_in_file(monkeypatch, tmp_path):
    p = tmp_path / "stats.json"
    p.write_text(json.dumps({"devices": {"A": []}}), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["netbox_checks.py", "-f", str(p), "--host", "MISSING"],
    )
    assert nc.main() == 1


def test_main_netbox_auth_error(monkeypatch, tmp_path):
    p = tmp_path / "stats.json"
    p.write_text(
        json.dumps({"devices": {"ALA-KZT-7280TR-1": [{"name": "eth1"}]}}),
        encoding="utf-8",
    )
    nb = type("NB", (), {})()
    nb.dcim = type("Dcim", (), {})()
    nb.dcim.devices = type("Dev", (), {})()
    nb.dcim.devices.filter = lambda **k: (_ for _ in ()).throw(Exception("403 Forbidden"))
    with patch.object(nc.pynetbox, "api", return_value=nb):
        monkeypatch.setattr(sys, "argv", ["netbox_checks.py", "-f", str(p)])
        assert nc.main() == 1


def test_load_mt_ref_invalid_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    values, lst, err = nc.load_mt_ref(str(bad))
    assert values is None
    assert err is not None
