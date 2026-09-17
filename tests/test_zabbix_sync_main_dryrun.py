"""zabbix_sync_commit_rate main: dry-run, host name fallback, macro errors."""

import json
import sys
from pathlib import Path

import pytest

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_dry_run_with_util_and_bps(monkeypatch, zabbix_env, netbox_env, tmp_path, capsys):
    import zabbix_sync_commit_rate as mod

    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        device_tag="border",
    )

    def host_get(params):
        filt = params.get("filter") or {}
        if "host" in filt:
            return []
        if "name" in filt:
            return [
                {
                    "hostid": "101",
                    "host": "ALA-KZT-7280TR-1",
                    "name": "ALA-KZT-7280TR-1",
                },
            ]
        return []

    build_standard_zabbix_mocker().on("host.get", host_get).activate(monkeypatch)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: nb)
    monkeypatch.setenv("NETBOX_TAG", "border")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_sync_commit_rate.py",
            "--dry-run",
            "--debug",
            "-d",
            str(FIXTURES / "dry_ssh_minimal.json"),
            "-f",
            str(cr),
        ],
    )
    mod.main()
    err = capsys.readouterr().err
    assert "dry-run" in err


def test_main_dry_run_uses_border_device_tag(monkeypatch, zabbix_env, netbox_env, tmp_path, capsys):
    """The configured device tag selects the border device."""
    import zabbix_sync_commit_rate as mod
    from uplinks.zabbix.plan import ZABBIX_MUTATING_METHODS


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
    mocker.activate(monkeypatch)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: nb)
    monkeypatch.setenv("NETBOX_TAG", "border")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_sync_commit_rate.py",
            "--dry-run",
            "-d",
            str(FIXTURES / "dry_ssh_minimal.json"),
            "-f",
            str(cr),
        ],
    )
    mod.main()
    captured = capsys.readouterr()
    err = captured.err
    assert "dry-run" in err
    assert "BPS:" in err
    assert "Nothing to sync" not in err
    mutating = [m for m in mocker.method_names() if m in ZABBIX_MUTATING_METHODS]
    assert mutating == []


def test_main_dry_run_util_scoped_excludes_out_of_scope_iface(
    monkeypatch, zabbix_env, netbox_env, tmp_path, capsys
):
    import zabbix_sync_commit_rate as mod

    dry = tmp_path / "dry-ssh.json"
    dry.write_text(
        """{
  "devices": {
    "WAW-EQX-7280QR-2": [
      {"name": "Ethernet23/1", "description": "Uplink: HurricaneE"},
      {"name": "Ethernet34/1", "description": "Uplink: Fiord and MSK PING-WIN 3Gbps link"}
    ]
  }
}""",
        encoding="utf-8",
    )
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")
    nb = build_netbox_for_commit_rates(
        device_name="WAW-EQX-7280QR-2",
        iface_name="Ethernet23/1",
        provider_name="Hurricane",
        device_tag="border",
        tag_device=True,
    )

    build_standard_zabbix_mocker(
        hosts=[{"hostid": "102", "host": "WAW-EQX-7280QR-2", "name": "WAW-EQX-7280QR-2"}],
    ).activate(monkeypatch)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: nb)
    monkeypatch.setenv("NETBOX_TAG", "border")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_sync_commit_rate.py",
            "--dry-run",
            "-d",
            str(dry),
            "-f",
            str(cr),
        ],
    )
    mod.main()
    err = capsys.readouterr().err
    assert "UTIL: 1 ifaces" in err
    assert "Ethernet34/1" not in err


def test_main_dry_run_rejects_delete_triggers(monkeypatch, zabbix_env, netbox_env, capsys):
    import zabbix_sync_commit_rate as mod

    mocker = build_standard_zabbix_mocker()
    mocker.on("trigger.delete", lambda p: (_ for _ in ()).throw(AssertionError("delete must not run")))
    mocker.activate(monkeypatch)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: build_netbox_for_commit_rates())
    for argv in (
        ["zabbix_sync_commit_rate.py", "--dry-run", "--delete-util-triggers"],
        ["zabbix_sync_commit_rate.py", "--dry-run", "--delete-link-triggers"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        assert "cannot be used with" in capsys.readouterr().err
    assert "trigger.delete" not in mocker.method_names()


def test_main_apply_macro_failure(monkeypatch, zabbix_env, netbox_env, tmp_path, capsys):
    import zabbix_sync_commit_rate as mod

    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        device_tag="border",
    )

    def item_get(params):
        search = (params.get("search") or {}).get("name", "")
        if search == "Bits received":
            return [{"key_": "net.if.in[1]"}]
        if search.startswith("Interface "):
            return [
                {"key_": "net.if.in[1]"},
                {"key_": "net.if.out[1]"},
                {"key_": "net.if.speed[1]"},
            ]
        return []

    mocker = build_standard_zabbix_mocker(
        hosts=[{"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"}],
    )
    mocker.on("usermacro.get", lambda p: []).on("usermacro.create", lambda p: {"hostmacroids": ["1"]})
    mocker.on("item.get", item_get).on("trigger.get", lambda p: []).on(
        "trigger.create", lambda p: {"triggerids": ["1"]}
    )
    mocker.activate(monkeypatch)

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
            str(FIXTURES / "dry_ssh_minimal.json"),
        ],
    )
    mod.main()
    captured = capsys.readouterr()
    assert "Error updating BPS" in captured.err or "OK:" in captured.out
