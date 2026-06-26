#!/usr/bin/env python3
"""Create or update NetBox circuits from commit_rates.json and dry-ssh.json."""

import sys

import uplinks.netbox.circuits as _impl

if __name__ == "__main__":
    _impl.main()

sys.modules[__name__] = _impl
