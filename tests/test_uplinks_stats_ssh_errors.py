"""uplinks_stats SSH connection and platform skip paths."""

from unittest.mock import MagicMock

import uplinks_stats as us


def test_get_ssh_uplinks_connect_failure(monkeypatch):
    class FailingClient:
        def set_missing_host_key_policy(self, _):
            pass

        def connect(self, hostname=None, **kwargs):
            raise OSError("connection refused")

    monkeypatch.setattr("uplinks_stats.paramiko.SSHClient", lambda: FailingClient())
    result, err = us.get_ssh_uplinks("host", "user", "pass", platform_name="Arista EOS")
    assert result is None
    assert err is not None

