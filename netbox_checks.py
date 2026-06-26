#!/usr/bin/env python3
"""Validate and optionally sync NetBox interfaces against dry-ssh.json."""

import sys

import uplinks.netbox.checks as _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())

sys.modules[__name__] = _impl
