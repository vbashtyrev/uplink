"""zabbix_sync main apply path with remove_threshold_items."""

import json
import sys
from pathlib import Path

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_sync_commit_rate import THRESHOLD_ITEM_KEY

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_apply_removes_threshold_items(monkeypatch, zabbix_env, netbox_env, tmp_path, capsys):
    import zabbix_sync_commit_rate as mod

    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "ALA-KZT-7280TR-1": {
                    "Ethernet51/1": {
                        "provider": "Cogent",
                        "circuit_id": "CKT-1",
                        "billing_model": "Burst",
                        "commit_rate_gbps": 10,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        device_tag="border",
    )

    def item_get(params):
        search = params.get("search") or {}
        if search.get("key_") == THRESHOLD_ITEM_KEY:
            return [{"itemid": "900", "key_": THRESHOLD_ITEM_KEY + '["Eth1"]'}]
        if search.get("name") == "Bits received":
            return [{"key_": "net.if.in[1]"}]
        if (search.get("name") or "").startswith("Interface "):
            return [{"key_": "net.if.in[1]"}, {"key_": "net.if.out[1]"}]
        return []

    deleted = []
    mocker = build_standard_zabbix_mocker(
        hosts=[{"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"}],
    )
    mocker.on("usermacro.get", lambda p: []).on("usermacro.create", lambda p: {"hostmacroids": ["1"]})
    mocker.on("item.get", item_get).on("item.delete", lambda p: deleted.append(p) or True)
    mocker.on("trigger.get", lambda p: []).on("trigger.create", lambda p: {"triggerids": ["1"]})
    mocker.activate(monkeypatch)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: nb)
    monkeypatch.setattr(mod, "sync_uplink_utilization_for_host", lambda *a, **k: (0, 0, []))
    monkeypatch.setenv("NETBOX_TAG", "border")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_sync_commit_rate.py",
            "-d",
            str(FIXTURES / "dry_ssh_minimal.json"),
            "-f",
            str(cr),
            "--create-link-triggers",
            "--no-util-triggers",
        ],
    )
    mod.main()
    out = capsys.readouterr().out
    assert "OK:" in out
    assert deleted
    assert "removed" in out
