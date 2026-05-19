"""run_uplinks_full.py additional paths."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import run_uplinks_full as full

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_load_env_file_and_strip_quotes(tmp_path, monkeypatch):
    env = tmp_path / "urls.env"
    env.write_text('export NETBOX_URL="https://nb.example"\n# comment\nFOO=bar\n', encoding="utf-8")
    monkeypatch.delenv("NETBOX_URL", raising=False)
    n = full.load_env_file(str(env))
    assert n == 2
    assert os.environ.get("NETBOX_URL") == "https://nb.example"
    assert os.environ.get("FOO") == "bar"


def test_run_cmd_success_and_timeout(tmp_path, monkeypatch):
    out_file = tmp_path / "out.txt"

    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, out, err = full.run_cmd([sys.executable, "-c", "print(1)"], str(tmp_path))
    assert ok and out == "ok"

    def timeout_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout_run)
    ok, out, err = full.run_cmd([sys.executable], str(tmp_path), timeout=1)
    assert not ok and "timeout" in err.lower()

    def fake_run_file(argv, stdout=None, **kwargs):
        if stdout:
            stdout.write("file-out")
        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run_file)
    ok, out, err = full.run_cmd([sys.executable], str(tmp_path), capture_stdout_to_file=str(out_file))
    assert ok and out_file.read_text() == "file-out"


def test_main_fetch_step_writes_dry_ssh(monkeypatch, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "commit_rates.json")
    monkeypatch.setattr(full, "DEFAULT_DESC_MAP", "description_to_name.json")

    payload = '{"devices": {"h1": []}}'

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        return True, payload, ""

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
            location=None,
            stop_on_error=False,
            no_stop_on_error=True,
            report=None,
            timeout=60,
            env_file="urls.env",
            no_env_file=True,
        ),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0
    assert dry.exists()
    assert "h1" in dry.read_text(encoding="utf-8")


def test_main_cache_skip(monkeypatch, tmp_path, capsys):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text('{"devices": {}}', encoding="utf-8")
    import time as time_mod

    os.utime(dry, (time_mod.time(), time_mod.time()))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "cr.json")
    monkeypatch.setattr(full, "DEFAULT_DESC_MAP", "desc.json")
    monkeypatch.setattr(full, "run_cmd", lambda *a, **k: (True, "", ""))

    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: full.argparse.Namespace(
            no_fetch=False,
            from_file=False,
            refresh=False,
            dry_ssh="dry-ssh.json",
            commit_rates="cr.json",
            no_netbox_apply=True,
            no_burst_triggers=False,
            location=None,
            stop_on_error=False,
            no_stop_on_error=True,
            report=None,
            timeout=60,
            env_file="urls.env",
            no_env_file=True,
        ),
    )
    with pytest.raises(SystemExit):
        full.main()
    assert "cache" in capsys.readouterr().out.lower() or "SKIP" in capsys.readouterr().out
