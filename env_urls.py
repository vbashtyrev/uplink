#!/usr/bin/env python3
"""Shared loader for urls.env-style environment files."""

import os


def _strip_quotes(value):
    if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
        return value[1:-1]
    return value


def load_env_file(path, *, overwrite=False):
    """
    Load KEY=VALUE lines from an env file.
    Supports lines like `export KEY=VALUE`, comments and empty lines.

    When overwrite is False (default), existing process env vars are preserved.
    """
    loaded = 0
    if not path or not os.path.isfile(path):
        return loaded
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            if not overwrite and key in os.environ:
                continue
            os.environ[key] = _strip_quotes(val.strip())
            loaded += 1
    return loaded


def load_env_file_if_present(filename="urls.env"):
    """Load urls.env from the project root; skip keys already in os.environ."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, filename)
    return load_env_file(path, overwrite=False)
