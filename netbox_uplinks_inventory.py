#!/usr/bin/env python3
"""Read-only NetBox uplink inventory CLI."""

import sys

import uplinks.netbox.inventory as _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())

sys.modules[__name__] = _impl
