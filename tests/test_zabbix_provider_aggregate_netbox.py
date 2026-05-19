"""zabbix_provider_aggregate NetBox provider list helper."""

from unittest.mock import MagicMock, patch

from zabbix_provider_aggregate import _build_edges_with_keys, _get_providers_from_netbox, _sanitize_provider_name


def test_get_providers_no_env(monkeypatch, capsys):
    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    assert _get_providers_from_netbox("automatization", debug=True) == []
    assert "NETBOX_URL" in capsys.readouterr().err


def test_get_providers_success(monkeypatch):
    nb = MagicMock()
    nb.circuits.providers.filter.return_value = [
        type("P", (), {"name": "Cogent"})(),
        type("P", (), {"name": "Hurricane"})(),
    ]
    with patch("zabbix_provider_aggregate.pynetbox.api", return_value=nb):
        monkeypatch.setenv("NETBOX_URL", "https://nb.example")
        monkeypatch.setenv("NETBOX_TOKEN", "tok")
        names = _get_providers_from_netbox("automatization", debug=False)
    assert "Cogent" in names


def test_build_edges_dedup():
    devices = {
        "H1": [
            {"name": "ae5", "description": "Uplink: ISP", "isLag": True},
            {"name": "ae5.0", "description": "Uplink: ISP", "isLogical": True},
        ],
    }
    items = {("H1", "ae5.0"): {"bits_in": "in", "bits_out": "out"}}
    edges = _build_edges_with_keys(devices, {"H1": "1"}, items, {})
    assert len(edges) == 1
    assert edges[0][2] == "in"


def test_sanitize_provider_name():
    assert _sanitize_provider_name("Cogent/Level3") == "Cogent Level3"
    assert _sanitize_provider_name("") == ""
