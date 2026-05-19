"""zabbix_sync main: BPS macro update failure message."""

import json
import sys
from pathlib import Path

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_bps_macro_failure_logged(monkeypatch, zabbix_env, netbox_env, tmp_path, capsys):
    import zabbix_sync_commit_rate as mod

    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        device_tag="border",
    )
    mocker = build_standard_zabbix_mocker(
        hosts=[{"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"}],
    )
    mocker.on("usermacro.get", lambda p: []).activate(monkeypatch)
    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: nb)
    monkeypatch.setattr(
        mod,
        "set_zabbix_host_if_util_macros",
        lambda *a, **k: (False, "macro fail"),
    )
    monkeypatch.setattr(mod, "sync_uplink_utilization_for_host", lambda *a, **k: (0, 0, []))
    monkeypatch.setattr(mod, "remove_threshold_items", lambda *a, **k: (0, None))
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
            "--no-util-triggers",
        ],
    )
    mod.main()
    assert "macro fail" in capsys.readouterr().err or "BPS" in capsys.readouterr().err
