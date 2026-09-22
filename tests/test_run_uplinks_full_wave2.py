"""run_uplinks_full.py additional paths."""

import json
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
    n = full.load_env_file(str(env), overwrite=True)
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
