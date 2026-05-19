"""run_uplinks_full helper functions."""

from pathlib import Path
from unittest.mock import patch

import run_uplinks_full as ruf


def test_append_debug_and_write_report(tmp_path, capsys):
    log_path = tmp_path / "debug.log"
    ruf._append_debug(str(log_path), "step1", stdout="ok", ok=True)
    ruf._append_debug(str(log_path), "step2", skip_reason="skipped")
    text = log_path.read_text(encoding="utf-8")
    assert "step1" in text
    assert "SKIP" in text
    ruf._write_run_report(["line1", "line2"], str(tmp_path / "run.log"), str(tmp_path / "rep.txt"))
    assert (tmp_path / "run.log").read_text(encoding="utf-8") == "line1\nline2"


def test_run_cmd_timeout(monkeypatch):
    import subprocess

    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    with patch("run_uplinks_full.subprocess.run", raise_timeout):
        ok, out, err = ruf.run_cmd(["echo"], cwd=".", timeout=1)
    assert ok is False
    assert "timeout" in err.lower()


def test_run_cmd_not_found():
    ok, out, err = ruf.run_cmd(["/nonexistent/binary-xyz"], cwd=".")
    assert ok is False
    assert "not found" in err.lower() or "Executable" in err
