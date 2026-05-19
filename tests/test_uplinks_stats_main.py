"""Tests for uplinks_stats.main() paths."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.mocks.netbox_records import MutableNetBox, _Record
from tests.mocks.ssh_channel import arista_script_from_fixtures
from uplinks_stats import main, print_table

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


def test_main_read_file_default(tmp_path, monkeypatch, capsys):
    stats = {"devices": {"host1": [{"name": "Eth1", "description": "Uplink: X"}]}}
    f = tmp_path / "uplinks_stats.json"
    f.write_text(json.dumps(stats), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--from-file", str(f)])
    assert main() == 0
    assert "host1" in capsys.readouterr().out


def test_main_json_from_file(tmp_path, monkeypatch, capsys):
    stats = {"devices": {"h": []}}
    f = tmp_path / "s.json"
    f.write_text(json.dumps(stats), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--from-file", str(f), "--json"])
    assert main() == 0
    out = json.loads(capsys.readouterr().out)
    assert "devices" in out


def test_main_missing_file(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--from-file", "/no/such/file.json"])
    assert main() == 1


def test_print_table_error_row(capsys):
    print_table({"dev1": {"error": "SSH failed"}})
    assert "SSH failed" in capsys.readouterr().out


def test_main_fetch_arista(monkeypatch, netbox_env, ssh_env, capsys):
    nb = MutableNetBox()
    platform = _Record(id=1, name="Arista EOS")
    dev = _Record(id=10, name="ALA-R1", platform=platform)
    nb.dcim.devices._items.append(dev)

    script = arista_script_from_fixtures(FIXTURES)

    dev.tag = "border"
    monkeypatch.setattr("uplinks_stats.pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(
        "uplinks_stats.get_device_platform_name",
        lambda d, nb_api: "Arista EOS",
    )
    monkeypatch.setattr(
        "uplinks_stats.get_arista_uplink_stats",
        lambda host, user, password, **kw: (
            [{"name": "Ethernet51/1", "description": "Uplink: X", "bandwidth": 10}],
            None,
        ),
    )
    monkeypatch.setenv("PARALLEL_DEVICES", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["uplinks_stats.py", "--fetch", "--platform", "arista", "--host", "ALA-R1"],
    )
    assert main() == 0


def test_main_report_missing_ssh(monkeypatch, netbox_env):
    monkeypatch.delenv("SSH_USERNAME", raising=False)
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--report"])
    assert main() == 1
