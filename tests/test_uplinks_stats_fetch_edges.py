"""uplinks_stats --fetch edge cases."""

import sys
from unittest.mock import MagicMock, patch

import uplinks_stats as us


def test_fetch_no_devices(monkeypatch, netbox_env, ssh_env, capsys):
    nb = MagicMock()
    nb.dcim.devices.filter.return_value = []
    monkeypatch.setattr("uplinks_stats.pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--fetch", "--platform", "all"])
    assert us.main() == 0
    assert "No devices" in capsys.readouterr().out


def test_fetch_host_not_found(monkeypatch, netbox_env, ssh_env, capsys):
    dev = MagicMock()
    dev.name = "OTHER"
    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [dev]
    monkeypatch.setattr("uplinks_stats.pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--fetch", "--host", "MISSING"])
    assert us.main() == 1


def test_fetch_netbox_error(monkeypatch, netbox_env, ssh_env, capsys):
    nb = MagicMock()
    nb.dcim.devices.filter.side_effect = Exception("403 Forbidden")
    monkeypatch.setattr("uplinks_stats.pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--fetch"])
    assert us.main() == 1


def test_fetch_thread_exception(monkeypatch, netbox_env, ssh_env, capsys):
    dev = MagicMock()
    dev.name = "R1"
    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [dev]
    monkeypatch.setattr("uplinks_stats.pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(us, "get_device_platform_name", lambda d, nb: "Arista EOS")
    monkeypatch.setattr(
        us,
        "process_one_device_stats",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ssh fail")),
    )
    monkeypatch.setattr(us, "_load_ssh_config", lambda: None)
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--fetch", "--json", "--platform", "arista"])
    assert us.main() == 0
    assert "error" in capsys.readouterr().out.lower()
