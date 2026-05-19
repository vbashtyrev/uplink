"""uplinks_stats --report mode with mocks."""

import sys
from unittest.mock import MagicMock, patch

from uplinks_stats import _run_report, main


def test_run_report_table(monkeypatch, netbox_env, ssh_env, capsys):
    nb = MagicMock()
    dev = MagicMock()
    dev.name = "ALA-R1"
    dev.primary_ip4 = "203.0.113.1"
    nb.dcim.devices.filter.return_value = [dev]
    nb.ipam.ip_addresses.filter.return_value = []

    monkeypatch.setattr("uplinks_stats.pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(
        "uplinks_stats.process_one_device",
        lambda *a, **k: ("ALA-R1", "203.0.113.1", "ok", "ok"),
    )
    code = _run_report("border", ".example.com")
    assert code == 0
    assert "ALA-R1" in capsys.readouterr().out


def test_main_report(monkeypatch, netbox_env, ssh_env):
    monkeypatch.setattr("uplinks_stats._run_report", lambda t, s: 0)
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--report"])
    assert main() == 0
