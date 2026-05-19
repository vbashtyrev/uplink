"""Tests for env_urls.py and run_uplinks_full.py helpers."""

import os

import env_urls
from run_uplinks_full import _strip_quotes, load_env_file


def test_strip_quotes():
    assert _strip_quotes('"value"') == "value"
    assert _strip_quotes("'x'") == "x"
    assert _strip_quotes("plain") == "plain"


def test_load_env_file_overwrites(tmp_path, monkeypatch):
    env_path = tmp_path / "test.env"
    env_path.write_text('export FOO="from_file"\nBAR=baz\n', encoding="utf-8")
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.setenv("BAR", "existing")
    n = load_env_file(str(env_path))
    assert n == 2
    assert os.environ["FOO"] == "from_file"
    assert os.environ["BAR"] == "baz"


def test_load_env_file_if_present_skips_existing(monkeypatch, tmp_path):
    env_path = tmp_path / "urls.env"
    env_path.write_text("KEEP=from_file\nNEW=1\n", encoding="utf-8")
    monkeypatch.setenv("KEEP", "already_set")
    monkeypatch.delenv("NEW", raising=False)

    def fake_isfile(path):
        return path == str(env_path)

    def fake_join(base, name):
        if name == "urls.env":
            return str(env_path)
        return os.path.join(base, name)

    monkeypatch.setattr(env_urls.os.path, "isfile", fake_isfile)
    monkeypatch.setattr(env_urls.os.path, "join", fake_join)
    loaded = env_urls.load_env_file_if_present("urls.env")
    assert loaded == 1
    assert os.environ["KEEP"] == "already_set"
    assert os.environ["NEW"] == "1"
