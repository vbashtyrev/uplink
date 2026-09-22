"""zabbix_provider_sla main with Burst circuit rows."""

import sys
from pathlib import Path
from unittest.mock import patch

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_rpc import ZabbixRpcMocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_burst_circuits_table(monkeypatch, zabbix_env, tmp_path, capsys):
    import zabbix_provider_sla as mod

    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="Cogent",
        circuit_id="ALA-1",
        circuit_custom_fields={"billing_model": "Burst"},
        commit_rate_kbps=10_000_000,
        tag_device=False,
    )
    cr = tmp_path / "commit_rates.json"

    def host_get(params):
        filt = params.get("filter") or {}
        if "host" in filt:
            return [{"hostid": "101", "host": "ALA-KZT-7280TR-1"}]
        return []

    def trigger_get(params):
        if "hostids" in params:
            return [
                {
                    "triggerid": "sla1",
                    "description": "Interface Ethernet51/1: SLA breach",
                    "hosts": [{"hostid": "101"}],
                },
            ]
        return []

    (
        ZabbixRpcMocker()
        .on("host.get", host_get)
        .on("trigger.get", trigger_get)
        .on("event.get", lambda p: [{"clock": "1000", "value": "1"}, {"clock": "2000", "value": "0"}])
        .activate(monkeypatch)
    )

    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "zabbix_provider_sla.py",
                "-f",
                str(cr),
                "--days",
                "1",
                "--from-ts",
                "0",
                "--to-ts",
                "10000",
            ],
        )
        mod.main()
    out = capsys.readouterr().out
    assert "Burst circuits" in out
    assert "CKT-ALA-1" in out
