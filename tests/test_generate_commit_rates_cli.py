"""Integration test: generate_commit_rates.py writes expected structure."""

import json
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


def test_cli_generates_commit_rates(tmp_path):
    dry = FIXTURES / "dry_ssh_minimal.json"
    desc = tmp_path / "desc.json"
    desc.write_text(
        json.dumps({"Uplink: Cogent 10G": "Cogent", "Uplink: Hurricane": "Hurricane", "Uplink: Hurricane member": "Hurricane"}),
        encoding="utf-8",
    )
    out = tmp_path / "commit_rates.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "generate_commit_rates.py"),
            "-f",
            str(dry),
            "-m",
            str(desc),
            "-o",
            str(out),
            "--no-merge",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "_comment" in data
    assert "ALA-KZT-7280TR-1" in data
    assert data["ALA-KZT-7280TR-1"]["Ethernet51/1"]["provider"] == "Cogent"
    assert proc.stdout.strip().startswith("Written")
