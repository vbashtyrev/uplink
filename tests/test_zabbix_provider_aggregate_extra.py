"""zabbix_provider_aggregate host create and NetBox errors."""

from unittest.mock import MagicMock, patch

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from zabbix_provider_aggregate import (
    _create_or_update_calculated_item,
    _get_or_create_host,
    _get_providers_from_netbox,
    _sanitize_provider_name,
)


def test_get_providers_netbox_exception(capsys, monkeypatch):
    nb = MagicMock()
    nb.circuits.providers.filter.side_effect = RuntimeError("nb down")
    with patch("zabbix_provider_aggregate.pynetbox.api", return_value=nb):
        monkeypatch.setenv("NETBOX_URL", "https://nb.example")
        monkeypatch.setenv("NETBOX_TOKEN", "tok")
        assert _get_providers_from_netbox("automatization", debug=True) == []
    assert "nb down" in capsys.readouterr().err


def test_get_or_create_host_creates(monkeypatch):
    created = []
    (
        ZabbixRpcMocker()
        .on("hostgroup.get", lambda p: [{"groupid": "2", "name": "Uplinks"}])
        .on("host.get", lambda p: [])
        .on(
            "host.create",
            lambda p: created.append(p) or {"hostids": ["300"]},
        )
        .activate(monkeypatch)
    )
    hid, err = _get_or_create_host(
        "https://z.example/api_jsonrpc.php",
        "t",
        "Uplinks Cogent/Level3",
        "Uplinks",
    )
    assert err is None
    assert hid == "300"
    assert created[0]["host"] == _sanitize_provider_name("Uplinks Cogent/Level3")


def test_create_or_update_calculated_item_update(monkeypatch):
    (
        ZabbixRpcMocker()
        .on(
            "item.get",
            lambda p: [{"itemid": "50", "key_": "aggregate.bits.in[]"}],
        )
        .on("item.update", lambda p: True)
        .activate(monkeypatch)
    )
    itemid, err = _create_or_update_calculated_item(
        "https://z.example/api_jsonrpc.php",
        "t",
        "101",
        "aggregate.bits.in[]",
        "Aggregate in",
        "sum(//host/key)",
    )
    assert err is None
    assert itemid == "50"
