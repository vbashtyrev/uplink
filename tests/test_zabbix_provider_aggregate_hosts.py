"""zabbix_provider_aggregate host/group helpers."""

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_provider_aggregate import _get_or_create_host, _sanitize_provider_name


def test_get_or_create_host_existing(monkeypatch, zabbix_env):
    mocker = build_standard_zabbix_mocker()
    mocker.on("hostgroup.get", lambda p: [{"groupid": "5"}]).on(
        "host.get", lambda p: [{"hostid": "10", "host": "Provider Cogent"}]
    ).activate(monkeypatch)
    hid, err = _get_or_create_host("https://z/api", "t", "Cogent", "Uplinks")
    assert hid == "10"
    assert err is None


def test_get_or_create_host_creates(monkeypatch, zabbix_env):
    mocker = build_standard_zabbix_mocker()
    mocker.on("hostgroup.get", lambda p: [{"groupid": "5"}]).on("host.get", lambda p: []).on(
        "host.create", lambda p: {"hostids": ["99"]}
    ).activate(monkeypatch)
    hid, err = _get_or_create_host("https://z/api", "t", "NewProv", "Uplinks")
    assert hid == "99"


def test_get_or_create_host_no_group(monkeypatch, zabbix_env):
    build_standard_zabbix_mocker().on("hostgroup.get", lambda p: []).activate(monkeypatch)
    hid, err = _get_or_create_host("https://z/api", "t", "X", "MissingGroup")
    assert hid is None
    assert "group" in (err or "").lower()
