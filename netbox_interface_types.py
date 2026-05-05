#!/usr/bin/env python3
"""
Download the list of Netbox interface types from GitHub (choices.py), extract value and label,
save to a JSON file for future use.
"""

import argparse
import json
import re
import sys

import requests

# Branch in netbox-community/netbox repository: master or main possible
NETBOX_CHOICES_URLS = [
    "https://raw.githubusercontent.com/netbox-community/netbox/master/netbox/dcim/choices.py",
    "https://raw.githubusercontent.com/netbox-community/netbox/main/netbox/dcim/choices.py",
]


def _fetch_interface_types_from_github():
    """
    Download choices.py from the Netbox repository, extract from the InterfaceTypeChoices class
    all TYPE_* = 'value' and from CHOICES pairs (constant, label). Return a list of {value, label}.
    Tries the master and main branches.
    """
    text = None
    last_err = None
    for url in NETBOX_CHOICES_URLS:
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            text = r.text
            break
        except Exception as e:
            last_err = e
            continue
    if not text:
        print("Error downloading from GitHub: {}".format(last_err), file=sys.stderr)
        return []
    start = text.find("class InterfaceTypeChoices")
    if start == -1:
        print("Class InterfaceTypeChoices not found", file=sys.stderr)
        return []
    end = text.find("\nclass ", start + 1)
    block = text[start:end] if end != -1 else text[start:]
    # Constants: TYPE_XXX = 'value'
    const_to_value = {}
    for m in re.finditer(r"(TYPE_[A-Z0-9_]+)\s*=\s*['\"]([^'\"]+)['\"]", block):
        const_to_value[m.group(1)] = m.group(2).strip()
    # Pairs (constant, label) from CHOICES: (TYPE_XXX, _('Label')) or (TYPE_XXX, 'Label')
    const_to_label = {}
    for m in re.finditer(r"(TYPE_[A-Z0-9_]+)\s*,\s*_\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", block):
        const_to_label[m.group(1)] = m.group(2).strip()
    for m in re.finditer(r"(TYPE_[A-Z0-9_]+)\s*,\s*['\"]([^'\"]+)['\"]", block):
        if m.group(1) not in const_to_label:
            const_to_label[m.group(1)] = m.group(2).strip()
    # Collect by value (unique), label from const_to_label or humanize(value)
    def humanize(s):
        return s.upper().replace("-", " ").replace("_", " ").strip()
    by_value = {}
    for const, value in const_to_value.items():
        if not value:
            continue
        label = const_to_label.get(const) or humanize(value)
        by_value[value] = {"value": value, "label": label}
    return [by_value[v] for v in sorted(by_value)]


def main():
    parser = argparse.ArgumentParser(description="Download Netbox interface types from GitHub to JSON")
    parser.add_argument(
        "-o", "--output",
        default="netbox_interface_types.json",
        metavar="FILE",
        help="Path to the output JSON file (default netbox_interface_types.json)",
    )
    args = parser.parse_args()
    types = _fetch_interface_types_from_github()
    if not types:
        return 1
    out = {"interface_types": types}
    try:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print("Error writing file: {}".format(e), file=sys.stderr)
        return 1
    print("{} types written in {}".format(len(types), args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
