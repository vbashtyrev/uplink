"""run_uplinks_full --plan: read-only preview mode."""

import json
from pathlib import Path
import pytest

import run_uplinks_full as full

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _plan_ns(**overrides):
    base = dict(
        auto=False,
        plan=True,
        no_fetch=True,
        from_file=True,
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
    )
    base.update(overrides)
    return full.argparse.Namespace(**base)


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


def test_plan_success_no_mutating_zabbix_scripts(monkeypatch, tmp_path):
    _setup_tmp(monkeypatch, tmp_path)
    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        if "netbox_uplinks_inventory.py" in argv and capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                json.dumps(
                    {
                        "complete": [{"device": "ALA-KZT-7280TR-1", "interface": "Eth1", "provider": "P"}],
                        "incomplete": [],
                        "stats": {"complete": 1, "incomplete": 0, "providers": 1},
                    }
                ),
                encoding="utf-8",
            )
            return True, "", ""
        return True, "plan ok", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(full.argparse.ArgumentParser, "parse_args", lambda self: _plan_ns())
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    scripts = [c[1] for c in calls if len(c) > 1]
    assert "uplinks_stats.py" not in scripts
    mutating_zabbix_scripts = frozenset(
        {
            "zabbix_sync_commit_rate.py",
            "zabbix_provider_aggregate.py",
            "zabbix_map.py",
            "zabbix_uplinks_dashboard.py",
            "zabbix_provider_services.py",
        }
    )
    for mut in mutating_zabbix_scripts:
        assert mut not in scripts
    assert scripts.count("zabbix_uplinks_plan.py") == 1

    netbox_checks = next(c for c in calls if c[1] == "netbox_checks.py")
    assert "--apply" not in netbox_checks
    assert "--existing-only" in netbox_checks

    plan = next(c for c in calls if c[1] == "zabbix_uplinks_plan.py")
    assert plan[2:4] == ["-d", "dry-ssh.json"]
    assert "--inventory-file" in plan
    assert "-f" not in plan
    assert "--create-link-triggers" in plan

    inventory_calls = [c for c in calls if c[1] == "netbox_uplinks_inventory.py"]
    assert len(inventory_calls) == 1


def test_plan_omits_burst_flag_when_disabled(monkeypatch, tmp_path):
    _setup_tmp(monkeypatch, tmp_path)
    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        if "netbox_uplinks_inventory.py" in argv and capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                json.dumps({"complete": [{"device": "d"}], "incomplete": [], "stats": {"complete": 1}}),
                encoding="utf-8",
            )
            return True, "", ""
        return True, "", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(full.argparse.ArgumentParser, "parse_args", lambda self: _plan_ns(no_burst_triggers=True))
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    plan_argv = next(c for c in calls if c[1] == "zabbix_uplinks_plan.py")
    assert "--create-link-triggers" not in plan_argv


def test_plan_rejects_auto_before_work(monkeypatch, tmp_path):
    _setup_tmp(monkeypatch, tmp_path)
    calls = []

    def fake_run_cmd(*args, **kwargs):
        calls.append(args)
        return True, "", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: _plan_ns(auto=True),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 2
    assert calls == []


def test_plan_rejects_from_file_auto_before_work(monkeypatch, tmp_path):
    _setup_tmp(monkeypatch, tmp_path)
    calls = []

    def fake_run_cmd(*args, **kwargs):
        calls.append(args)
        return True, "", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: _plan_ns(auto=True, from_file=True, no_fetch=True),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 2
    assert calls == []


def test_plan_requires_from_file(monkeypatch, tmp_path):
    _setup_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(full, "run_cmd", lambda *a, **k: (True, "", ""))
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: _plan_ns(no_fetch=False, from_file=False),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 2


def test_plan_fails_on_incomplete_inventory(monkeypatch, tmp_path):
    _setup_tmp(monkeypatch, tmp_path)

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        if capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                json.dumps(
                    {
                        "complete": [],
                        "incomplete": [{"provider": "P", "circuit_id": "C", "reason": "no_cable"}],
                        "stats": {"complete": 0, "incomplete": 1},
                    }
                ),
                encoding="utf-8",
            )
            return True, "", ""
        return True, "", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(full.argparse.ArgumentParser, "parse_args", lambda self: _plan_ns())
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 1


def test_plan_fails_on_auth_denied_inventory(monkeypatch, tmp_path):
    _setup_tmp(monkeypatch, tmp_path)

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        if capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                json.dumps(
                    {
                        "complete": [],
                        "incomplete": [],
                        "stats": {"error": "auth_denied"},
                    }
                ),
                encoding="utf-8",
            )
            return True, "", ""
        return True, "", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(full.argparse.ArgumentParser, "parse_args", lambda self: _plan_ns())
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 1
