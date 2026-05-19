"""uplinks_stats SSH config resolution."""

from unittest.mock import MagicMock

import uplinks_stats as us


def test_load_ssh_config_missing(monkeypatch):
    monkeypatch.setattr(us.os.path, "expanduser", lambda p: "/nonexistent/config")
    monkeypatch.setattr(us.os.path, "isfile", lambda p: False)
    assert us._load_ssh_config() is None


def test_resolve_ssh_host_with_config():
    cfg = MagicMock()
    cfg.lookup.return_value = {"hostname": "real.host", "user": "admin2"}
    host, user = us._resolve_ssh_host(cfg, "dev1", "dev1.example.com", "user1")
    assert host == "real.host"
    assert user == "admin2"


def test_resolve_ssh_host_fallback():
    host, user = us._resolve_ssh_host(None, "dev", "dev.example.com", "u")
    assert host == "dev.example.com"
    assert user == "u"
