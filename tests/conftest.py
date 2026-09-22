"""Shared pytest configuration and fixtures."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# uplinks_config.py is gitignored (local copy of the example). CI / fresh clones need a fallback.
_config = ROOT / "uplinks_config.py"
_example = ROOT / "uplinks_config.example.py"
if not _config.exists() and _example.exists():
    _config.write_text(_example.read_text(encoding="utf-8"), encoding="utf-8")
elif _config.exists() and _example.exists():
    example_text = _example.read_text(encoding="utf-8")
    config_text = _config.read_text(encoding="utf-8")
    for line in example_text.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        name = line.split("=", 1)[0].strip()
        if name and name + " =" not in config_text and name + "=" not in config_text:
            config_text = config_text.rstrip() + "\n\n" + line + "\n"
    _config.write_text(config_text, encoding="utf-8")

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Speed up SSH/read loops that use time.sleep."""
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _clear_netbox_incomplete_guard():
    """Reset Zabbix transport guard so tests cannot leak process state."""
    from uplinks.zabbix.client import clear_incomplete_netbox_data

    clear_incomplete_netbox_data()
    yield
    clear_incomplete_netbox_data()


@pytest.fixture
def zabbix_env(monkeypatch):
    monkeypatch.setenv("ZABBIX_URL", "https://zabbix.example/api_jsonrpc.php")
    monkeypatch.setenv("ZABBIX_TOKEN", "test-token")


@pytest.fixture
def netbox_env(monkeypatch):
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example")
    monkeypatch.setenv("NETBOX_TOKEN", "nb-token")
    monkeypatch.setenv("NETBOX_TAG", "border")


@pytest.fixture
def ssh_env(monkeypatch):
    monkeypatch.setenv("SSH_USERNAME", "admin")
    monkeypatch.setenv("SSH_PASSWORD", "secret")
    monkeypatch.setenv("SSH_HOST_SUFFIX", ".example.com")
    monkeypatch.setenv("USE_SSH_CONFIG", "0")
