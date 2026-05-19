"""zabbix_provider_sla main with Burst circuit rows."""

import json
import sys
from pathlib import Path

from tests.mocks.zabbix_rpc import ZabbixRpcMocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_burst_circuits_table(monkeypatch, zabbix_env, tmp_path, capsys):
    import zabbix_provider_sla as mod

    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "_provider_sla": 99.0,
                "ALA-KZT-7280TR-1": {
                    "Ethernet51/1": {
                        "provider": "Cogent",
                        "circuit_id": "CKT-ALA-1",
                        "billing_model": "Burst",
                        "commit_rate_gbps": 10,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

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

    monkeypatch.setattr(
        sys,
        "argv",
        ["zabbix_provider_sla.py", "-f", str(cr), "--days", "1", "--from-ts", "0", "--to-ts", "10000"],
    )
    mod.main()
    out = capsys.readouterr().out
    assert "Burst circuits" in out
    assert "CKT-ALA-1" in out
