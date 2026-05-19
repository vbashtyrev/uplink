"""Zabbix JSON-RPC tests with mocked requests.post."""

import pytest

from tests.mocks.zabbix_rpc import ZabbixRpcMocker, mock_response
from zabbix_map import validate_zabbix_token, zabbix_request


def test_zabbix_request_success(monkeypatch):
    mocker = ZabbixRpcMocker()
    mocker.on("user.get", lambda p: [{"userid": "1"}])
    mocker.activate(monkeypatch)

    result, err = zabbix_request("https://zabbix.example/api_jsonrpc.php", "token", "user.get", {"limit": 1})
    assert err is None
    assert result == [{"userid": "1"}]
    assert mocker.method_names() == ["user.get"]


def test_zabbix_request_api_error(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return mock_response(error={"code": -32602, "message": "Invalid params", "data": "hostids"})

    monkeypatch.setattr("requests.post", fake_post)
    result, err = zabbix_request("https://zabbix.example/api_jsonrpc.php", "t", "host.get", {})
    assert result is None
    assert "hostids" in err


def test_validate_zabbix_token_ok(monkeypatch):
    ZabbixRpcMocker().on("user.get", lambda p: []).activate(monkeypatch)
    ok, err = validate_zabbix_token("https://zabbix.example/api_jsonrpc.php", "t")
    assert ok is True
    assert err is None


def test_validate_zabbix_token_fail(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return mock_response(error={"code": -32602, "message": "Session expired", "data": ""})

    monkeypatch.setattr("requests.post", fake_post)
    ok, err = validate_zabbix_token("https://zabbix.example/api_jsonrpc.php", "bad")
    assert ok is False
    assert err  # zabbix_map prefers error.data in message; empty data still fails validation
