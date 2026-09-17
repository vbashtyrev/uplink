"""run_uplinks_full: human inventory-first orchestration."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import run_uplinks_full as full
from uplinks.netbox.inventory import ERROR_AUTH_DENIED

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _inventory_json(complete_devices, incomplete=None, stats=None):
    payload = {
        "complete": [
            {
                "provider": "Cogent",
                "circuit_id": "CKT-{}".format(i + 1),
                "device": name,
                "interface": "Eth1",
                "commit_rate_kbps": 10000,
                "billing_model": "flat",
            }
            for i, name in enumerate(complete_devices)
        ],
        "incomplete": incomplete or [],
        "stats": stats or {
            "complete": len(complete_devices),
            "incomplete": len(incomplete or []),
        },
    }
    return json.dumps(payload)


def _setup_tmp(monkeypatch, tmp_path):
    (tmp_path / "commit_rates.json").write_text("{}", encoding="utf-8")
    (tmp_path / "description_to_name.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "commit_rates.json")
    monkeypatch.setattr(full, "DEFAULT_DESC_MAP", "description_to_name.json")
    monkeypatch.setattr(full, "DEFAULT_NETBOX_INVENTORY", "netbox_inventory.json")


def _human_ns(**overrides):
    base = dict(
        auto=False,
        plan=False,
        no_fetch=False,
        from_file=False,
        refresh=False,
        dry_ssh="dry-ssh.json",
        commit_rates="commit_rates.json",
        no_netbox_apply=False,
        no_burst_triggers=False,
        location=None,
        stop_on_error=True,
        no_stop_on_error=False,
        report=None,
        timeout=60,
        env_file="urls.env",
        no_env_file=True,
        netbox_checks=False,
    )
    base.update(overrides)
    return full.argparse.Namespace(**base)


def test_human_mode_skips_ssh_even_without_from_file(monkeypatch, tmp_path):
    """Human path never calls uplinks_stats; inventory step 0 drives Zabbix."""
    _setup_tmp(monkeypatch, tmp_path)
    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        if "netbox_uplinks_inventory.py" in argv and capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                _inventory_json(["ALA-KZT-7280TR-1"]),
                encoding="utf-8",
            )
        return True, "ok", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(full.argparse.ArgumentParser, "parse_args", lambda self: _human_ns())
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    scripts = [c[1] for c in calls]
    assert scripts[0] == "netbox_uplinks_inventory.py"
    assert "uplinks_stats.py" not in scripts
    assert (tmp_path / "netbox_inventory.json").is_file()


def test_auto_fetch_does_not_use_inventory_file(monkeypatch, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    _setup_tmp(monkeypatch, tmp_path)
    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        if "uplinks_stats.py" in argv:
            return True, json.dumps({"devices": {}}), ""
        return True, "ok", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: full.argparse.Namespace(
            auto=True,
            plan=False,
            no_fetch=False,
            from_file=False,
            refresh=True,
            dry_ssh="dry-ssh.json",
            commit_rates="commit_rates.json",
            no_netbox_apply=False,
            no_burst_triggers=False,
            location=None,
            stop_on_error=True,
            no_stop_on_error=False,
            report=None,
            timeout=60,
            env_file="urls.env",
            no_env_file=True,
            netbox_checks=False,
        ),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    scripts = [c[1] for c in calls]
    assert scripts[0] == "uplinks_stats.py"
    assert "netbox_uplinks_inventory.py" not in scripts
    stats = calls[0]
    assert "--inventory-file" not in stats


def test_human_inventory_auth_failure_stops(monkeypatch, tmp_path):
    _setup_tmp(monkeypatch, tmp_path)

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        if capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                json.dumps({"complete": [], "incomplete": [], "stats": {"error": ERROR_AUTH_DENIED}}),
                encoding="utf-8",
            )
        return True, "", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(full.argparse.ArgumentParser, "parse_args", lambda self: _human_ns())
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 1
