"""run_uplinks_full with optional netbox_checks step."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import run_uplinks_full as full

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_with_netbox_checks_flag(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_NETBOX_INVENTORY", "netbox_inventory.json")

    steps = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        steps.append(" ".join(str(x) for x in argv))
        if "netbox_uplinks_inventory.py" in argv and capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                json.dumps(
                    {
                        "complete": [{"device": "h1", "interface": "Eth1", "provider": "P"}],
                        "incomplete": [],
                        "stats": {"complete": 1},
                    }
                ),
                encoding="utf-8",
            )
        return True, "ok", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: full.argparse.Namespace(
            plan=False,
            dry_ssh="dry-ssh.json",
            no_netbox_apply=False,
            no_burst_triggers=False,
            stop_on_error=False,
            no_stop_on_error=True,
            report=None,
            timeout=60,
            env_file="urls.env",
            no_env_file=True,
            netbox_checks=True,
        ),
    )
    with patch.object(full, "_write_run_report"):
        with pytest.raises(SystemExit) as exc:
            full.main()
    assert exc.value.code == 0
    assert any("netbox_checks" in s for s in steps)
    assert any("--scope-to-file" in s for s in steps)
    assert any("--existing-only" in s for s in steps)
