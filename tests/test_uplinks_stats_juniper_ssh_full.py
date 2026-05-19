"""Full get_juniper_uplink_stats path (scripted SSH)."""

import json
from pathlib import Path

from tests.mocks.ssh_channel import FakeChannel, FakeSSHClient, juniper_logical_uplink_script
from uplinks_stats import get_juniper_uplink_stats, parse_juniper_uplinks

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_get_juniper_uplink_stats_scripted(monkeypatch):
    script = juniper_logical_uplink_script(FIXTURES)
    monkeypatch.setattr(
        "uplinks_stats.paramiko.SSHClient",
        lambda: FakeSSHClient(FakeChannel(script)),
    )
    stats, err = get_juniper_uplink_stats("10.0.0.1", "admin", "pass", log=None)
    assert err is None
    assert isinstance(stats, list)
    assert len(stats) >= 1
    names = {r["name"] for r in stats}
    uplinks = parse_juniper_uplinks(
        json.loads((FIXTURES / "juniper_descriptions.json").read_text(encoding="utf-8")),
        require_link_up=True,
    )
    assert names & {n for n, _ in uplinks}
