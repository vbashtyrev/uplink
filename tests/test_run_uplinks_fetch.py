"""run_uplinks_full: fetch step and refresh."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import run_uplinks_full as full

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_fetch_step(monkeypatch, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "commit_rates.json")
    monkeypatch.setattr(full, "DEFAULT_DESC_MAP", "description_to_name.json")

    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(" ".join(str(x) for x in argv))
        if "uplinks_stats" in str(argv):
            if capture_stdout_to_file:
                Path(capture_stdout_to_file).write_text(
                    json.dumps({"devices": {"ALA-KZT-7280TR-1": []}}),
                    encoding="utf-8",
                )
        return True, "ok", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: full.argparse.Namespace(
            no_fetch=False,
            from_file=False,
            refresh=True,
            dry_ssh="dry-ssh.json",
            commit_rates="commit_rates.json",
            no_netbox_apply=True,
            no_burst_triggers=False,
            location="ALA",
            stop_on_error=False,
            no_stop_on_error=True,
            report=None,
            timeout=60,
            env_file="urls.env",
            no_env_file=True,
        ),
    )
    with patch.object(full, "_write_run_report"):
        with pytest.raises(SystemExit) as exc:
            full.main()
    assert exc.value.code == 0
    assert any("uplinks_stats" in str(c) for c in calls)
