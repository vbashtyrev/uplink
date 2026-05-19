"""zabbix_provider_sla: aggregate providers table with SLA below target."""

import json
import sys
from pathlib import Path

from tests.mocks.zabbix_rpc import ZabbixRpcMocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_aggregate_providers_below_sla(monkeypatch, zabbix_env, tmp_path, capsys):
    import zabbix_provider_sla as mod

    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "_provider_limits": {"Cogent": 10},
                "_provider_sla": 99.99,
            }
        ),
        encoding="utf-8",
    )
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
