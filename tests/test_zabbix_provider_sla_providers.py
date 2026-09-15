"""zabbix_provider_sla: aggregate providers table with SLA below target."""

import sys
from pathlib import Path
from unittest.mock import patch

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_rpc import ZabbixRpcMocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_aggregate_providers_below_sla(monkeypatch, zabbix_env, tmp_path, capsys):
    import zabbix_provider_sla as mod

    nb = build_netbox_for_commit_rates(
        device_name="ALA-R1",
        iface_name="Eth1",
        provider_name="Cogent",
        provider_custom_fields={"aggregate_limit_gbps": 10, "slo_percent": 99.99},
        tag_device=False,
    )
    cr = tmp_path / "commit_rates.json"
    agg = mod.UPLINKS_AGGREGATE_HOST_PREFIX + "Cogent"

    def host_get(params):
        return [{"hostid": "50", "host": agg, "name": agg}]

    def trigger_get(params):
        if "hostids" in params:
            return [
                {
                    "triggerid": "sla1",
                    "description": "Provider aggregate SLA breach: Cogent",
                    "hosts": [{"hostid": "50"}],
                },
            ]
        return []

    (
        ZabbixRpcMocker()
        .on("host.get", host_get)
        .on("trigger.get", trigger_get)
        .on(
            "event.get",
            lambda p: [
                {"clock": "0", "value": "1"},
                {"clock": "5000", "value": "0"},
            ],
        )
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
                "--from-ts",
                "0",
                "--to-ts",
                "10000",
            ],
        )
        mod.main()
    out = capsys.readouterr().out
    assert "Aggregate providers" in out
    assert "Cogent" in out
