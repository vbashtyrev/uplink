"""uplinks_stats --report with mocked NetBox and process_one_device."""

import sys
from unittest.mock import MagicMock, patch

import uplinks_stats as us


def test_run_report_processes_devices(monkeypatch, netbox_env, ssh_env, capsys):
    dev = MagicMock()
    dev.name = "R1"
    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [dev]
    monkeypatch.setenv("NETBOX_URL", "https://nb.example")
    monkeypatch.setenv("NETBOX_TOKEN", "tok")
    monkeypatch.setenv("SSH_USERNAME", "admin")
    monkeypatch.setenv("SSH_PASSWORD", "pass")
    monkeypatch.setattr("uplinks_stats.pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(
        us,
        "process_one_device",
        lambda *a, **k: ("R1", "1.2.3.4", "nb-cell", "ssh-cell"),
    )
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--report"])
    assert us.main() == 0
    assert "R1" in capsys.readouterr().out
