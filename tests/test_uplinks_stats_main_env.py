"""uplinks_stats main() environment validation paths."""

import sys
from unittest.mock import patch

import uplinks_stats as us


def test_main_report_missing_netbox(monkeypatch, capsys):
    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--report"])
    assert us.main() == 1
    assert "NETBOX" in capsys.readouterr().out + capsys.readouterr().err


def test_main_fetch_missing_ssh_user(monkeypatch, capsys):
    monkeypatch.setenv("NETBOX_URL", "https://nb.example")
    monkeypatch.setenv("NETBOX_TOKEN", "tok")
    monkeypatch.delenv("SSH_USERNAME", raising=False)
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--fetch"])
    assert us.main() == 1


def test_main_report_runs(monkeypatch):
    monkeypatch.setenv("NETBOX_URL", "https://nb.example")
    monkeypatch.setenv("NETBOX_TOKEN", "tok")
    monkeypatch.setenv("SSH_USERNAME", "admin")
    monkeypatch.setenv("SSH_PASSWORD", "pass")
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--report"])
    with patch.object(us, "_run_report", return_value=0):
        assert us.main() == 0
