"""Shared pytest configuration and fixtures."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Speed up SSH/read loops that use time.sleep."""
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)


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
