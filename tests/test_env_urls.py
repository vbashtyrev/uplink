"""env_urls.load_env_file_if_present."""

import os
from unittest.mock import patch

import env_urls


def test_load_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "urls.env"
    env_file.write_text(
        'export EXISTING=1\nNEW_VAR="quoted"\n# comment\nBADLINE\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("NEW_VAR", raising=False)
    monkeypatch.setenv("EXISTING", "already")
    with patch.object(env_urls.os.path, "isfile", return_value=True):
        with patch.object(env_urls.os.path, "join", return_value=str(env_file)):
            loaded = env_urls.load_env_file_if_present("urls.env")
    assert loaded == 1
    assert os.environ.get("NEW_VAR") == "quoted"
    assert os.environ.get("EXISTING") == "already"


def test_load_env_missing_file(monkeypatch):
    with patch.object(env_urls.os.path, "isfile", return_value=False):
        assert env_urls.load_env_file_if_present("missing.env") == 0
