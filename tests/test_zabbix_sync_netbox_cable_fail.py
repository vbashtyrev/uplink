"""get_commit_rates_from_netbox when cable.get fails."""

from tests.mocks.netbox_api import MockNetBox, _Record, build_netbox_for_commit_rates


def test_cable_get_failure_skips(monkeypatch):
    nb = build_netbox_for_commit_rates()
    nb.dcim.cables.get = lambda pk: (_ for _ in ()).throw(RuntimeError("api error"))
    from zabbix_sync_commit_rate import get_commit_rates_from_netbox

    result = get_commit_rates_from_netbox(nb, tag="uplinks", debug=True)
    assert result == {}
