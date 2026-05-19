"""uplinks_stats _load_ssh_config with real temp file."""

import uplinks_stats as us


def test_load_ssh_config_parses_file(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config"
    cfg_path.write_text(
        "Host dev1\n  HostName real.example.com\n  User admin\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(us.os.path, "expanduser", lambda p: str(cfg_path))
    monkeypatch.setattr(us.os.path, "isfile", lambda p: True)
    cfg = us._load_ssh_config()
    assert cfg is not None
    host, user = us._resolve_ssh_host(cfg, "dev1", "dev1", "fallback")
    assert host == "real.example.com"
    assert user == "admin"
