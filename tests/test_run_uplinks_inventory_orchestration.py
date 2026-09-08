"""run_uplinks_full: human pre-SSH inventory and --inventory-file orchestration."""

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
    dry = tmp_path / "dry-ssh.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "commit_rates.json").write_text("{}", encoding="utf-8")
    (tmp_path / "description_to_name.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "commit_rates.json")
    monkeypatch.setattr(full, "DEFAULT_DESC_MAP", "description_to_name.json")
    monkeypatch.setattr(full, "DEFAULT_NETBOX_INVENTORY", "netbox_inventory.json")
    return dry


def test_human_fetch_runs_inventory_before_ssh_with_inventory_file(monkeypatch, tmp_path):
    _setup_tmp(monkeypatch, tmp_path)
    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        if "netbox_uplinks_inventory.py" in argv and capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                _inventory_json(["ALA-KZT-7280TR-1"]),
                encoding="utf-8",
            )
            return False, "", "incomplete entries present"
        if "uplinks_stats.py" in argv:
            return True, json.dumps({"devices": {"ALA-KZT-7280TR-1": []}}), ""
        return True, "ok", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: full.argparse.Namespace(
            auto=False,
            no_fetch=False,
            from_file=False,
            refresh=True,
            dry_ssh="dry-ssh.json",
            commit_rates="commit_rates.json",
            no_netbox_apply=True,
            no_burst_triggers=False,
            location=None,
            stop_on_error=True,
            no_stop_on_error=False,
            report=None,
            timeout=60,
            env_file="urls.env",
            no_env_file=True,
        ),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    scripts = [c[1] for c in calls]
    assert scripts[0] == "netbox_uplinks_inventory.py"
    assert scripts[1] == "uplinks_stats.py"

    pre_ssh = calls[0]
    assert "--json" in pre_ssh
    assert "--dry-run" in pre_ssh

    stats = calls[1]
    assert "--inventory-file" in stats
    inv_idx = stats.index("--inventory-file")
    assert stats[inv_idx + 1] == str(tmp_path / "netbox_inventory.json")

    assert (tmp_path / "netbox_inventory.json").is_file()


def test_auto_fetch_does_not_use_inventory_file(monkeypatch, tmp_path):
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
            no_fetch=False,
            from_file=False,
            refresh=True,
            dry_ssh="dry-ssh.json",
            commit_rates="commit_rates.json",
            no_netbox_apply=True,
            no_burst_triggers=False,
            location=None,
            stop_on_error=True,
            no_stop_on_error=False,
            report=None,
            timeout=60,
            env_file="urls.env",
            no_env_file=True,
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


def test_human_no_fetch_skips_pre_ssh_inventory(monkeypatch, tmp_path):
    _setup_tmp(monkeypatch, tmp_path)
    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        return True, "ok", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: full.argparse.Namespace(
            auto=False,
            no_fetch=True,
            from_file=False,
            refresh=False,
            dry_ssh="dry-ssh.json",
            commit_rates="commit_rates.json",
            no_netbox_apply=True,
            no_burst_triggers=False,
            location=None,
            stop_on_error=True,
            no_stop_on_error=False,
            report=None,
            timeout=60,
            env_file="urls.env",
            no_env_file=True,
        ),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    scripts = [c[1] for c in calls]
    assert "uplinks_stats.py" not in scripts
    pre_ssh = [c for c in calls if c[1] == "netbox_uplinks_inventory.py" and "--json" in c]
    assert pre_ssh == []
    step3 = next(c for c in calls if c[1] == "netbox_uplinks_inventory.py")
    assert "--dry-run" in step3
    assert "--json" not in step3


def test_human_fetch_auth_error_stops_before_ssh(monkeypatch, tmp_path):
    _setup_tmp(monkeypatch, tmp_path)
    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        if "netbox_uplinks_inventory.py" in argv and capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                _inventory_json([], stats={"error": ERROR_AUTH_DENIED}),
                encoding="utf-8",
            )
            return False, "", "auth denied"
        return True, "", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: full.argparse.Namespace(
            auto=False,
            no_fetch=False,
            from_file=False,
            refresh=True,
            dry_ssh="dry-ssh.json",
            commit_rates="commit_rates.json",
            no_netbox_apply=True,
            no_burst_triggers=False,
            location=None,
            stop_on_error=True,
            no_stop_on_error=False,
            report=None,
            timeout=60,
            env_file="urls.env",
            no_env_file=True,
        ),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 1

    scripts = [c[1] for c in calls]
    assert scripts == ["netbox_uplinks_inventory.py"]
    assert "uplinks_stats.py" not in scripts
