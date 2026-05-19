"""read_until, read_until_json_and_prompt, read_until_prompt."""

import json

from tests.mocks.ssh_channel import FakeChannel
from uplinks_stats import read_until, read_until_json_and_prompt, read_until_prompt


def test_read_until_json_and_prompt_extracts():
    payload = {"interfaces": [{"name": "Eth1"}]}
    text = 'noise\n' + json.dumps(payload) + '\nadmin@router# '
    ch = FakeChannel([text])
    data = read_until_json_and_prompt(ch, timeout=5)
    assert data == payload


def test_read_until_prompt_detects_cli():
    ch = FakeChannel(['<xml>partial\n', 'admin@mx1> '])
    out = read_until_prompt(ch, timeout=5)
    assert 'admin@mx1>' in out


def test_read_until_matches_pattern():
    ch = FakeChannel(['line1\n', 'router# '])
    out = read_until(ch, ['#'], max_wait=5)
    assert '#' in out
