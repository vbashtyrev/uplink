"""zabbix_uplinks_dashboard NetBox provider fetch."""

from unittest.mock import MagicMock, patch

from zabbix_uplinks_dashboard import _get_providers_from_netbox


def test_dashboard_providers_no_env(monkeypatch, capsys):
    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    assert _get_providers_from_netbox("automatization", debug=True) == []
    assert "NETBOX" in capsys.readouterr().err


def test_dashboard_providers_success(monkeypatch):
    nb = MagicMock()
    nb.circuits.providers.filter.return_value = [type("P", (), {"name": "Cogent"})()]
    with patch("zabbix_uplinks_dashboard.pynetbox.api", return_value=nb):
        monkeypatch.setenv("NETBOX_URL", "https://nb.example")
        monkeypatch.setenv("NETBOX_TOKEN", "tok")
        assert _get_providers_from_netbox("automatization") == ["Cogent"]
