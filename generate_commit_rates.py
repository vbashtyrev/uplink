#!/usr/bin/env python3
"""Generate or merge commit_rates.json from dry-ssh.json and description_to_name mapping."""

import argparse
import json
import os
import sys

DEFAULT_DRY_SSH = "dry-ssh.json"
DEFAULT_DESC_MAP = "description_to_name.json"
DEFAULT_OUTPUT = "commit_rates.json"


def load_json(path, default=None):
    """Documentation."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        return (None, str(e))


def is_uplink(iface):
    """Documentation."""
    desc = (iface.get("description") or "").strip()
    return "Uplink:" in desc


def location_from_hostname(hostname):
    """Documentation."""
    parts = (hostname or "").split("-")
    return parts[0] if parts and parts[0] else (hostname or "other")


def build_circuit_id_map(data, desc_to_provider, existing):
    """Documentation."""
    rows = []
    for dev_name in sorted(data["devices"].keys()):
        ifaces = data["devices"].get(dev_name)
        if not isinstance(ifaces, list):
            continue
        for iface in ifaces:
            name = (iface.get("name") or "").strip()
            if not name or not is_uplink(iface):
                continue
            desc = (iface.get("description") or "").strip()
            provider = desc_to_provider.get(desc, desc or "")
            if provider and provider == desc and len(desc) > 30:
                provider = provider[:27] + "..."
            provider = (provider or "").strip() or "Uplink"
            loc = location_from_hostname(dev_name)
            entry = (existing.get(dev_name) or {}).get(name)
            rows.append((provider, loc, dev_name, name, entry))

    rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))  # provider, location, dev, iface
    #
    counter = {}
    cid_map = {}
    for provider, loc, dev_name, iface_name, entry in rows:
        key = (provider, loc)
        counter[key] = counter.get(key, 0) + 1
        num = counter[key]
        default_cid = "{}-{}-{}".format(provider, loc, num)
        existing_cid = entry.get("circuit_id") if isinstance(entry, dict) else None
        cid_map[(dev_name, iface_name)] = (existing_cid or "").strip() or default_cid
    return cid_map


def main():
    parser = argparse.ArgumentParser(
        description="message",
    )
    parser.add_argument("-f", "--file", default=DEFAULT_DRY_SSH, help="message")
    parser.add_argument("-m", "--description-map", default=DEFAULT_DESC_MAP, help="message")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT, help="message")
    parser.add_argument("--no-merge", action="store_true", help="message")
    args = parser.parse_args()

    data = load_json(args.file)
    if isinstance(data, tuple):
        print("message".format(args.file, data[1]), file=sys.stderr)
        sys.exit(1)
    if data is None:
        print("message".format(args.file), file=sys.stderr)
        sys.exit(1)
    if "devices" not in data:
        print("message", file=sys.stderr)
        sys.exit(1)

    desc_to_provider = load_json(args.description_map) if os.path.isfile(args.description_map) else {}
    if isinstance(desc_to_provider, tuple):
        print("message".format(args.description_map, desc_to_provider[1]), file=sys.stderr)
        sys.exit(1)

    existing = {}
    existing_meta = {}
    provider_limits = None  #
    provider_sla = None     #
    if not args.no_merge and os.path.isfile(args.output):
        existing_raw = load_json(args.output)
        if isinstance(existing_raw, tuple):
            print("message".format(args.output, existing_raw[1]), file=sys.stderr)
            sys.exit(1)
        if isinstance(existing_raw, dict):
            provider_limits = existing_raw.get("_provider_limits")
            if not isinstance(provider_limits, dict):
                provider_limits = None
            provider_sla = existing_raw.get("_provider_sla")
            if not isinstance(provider_sla, (int, float)):
                provider_sla = None
            #
            #
            for k, v in existing_raw.items():
                if isinstance(k, str) and k.startswith("_") and k not in ("_comment", "_provider_limits", "_provider_sla"):
                    existing_meta[k] = v
        existing = {k: v for k, v in (existing_raw or {}).items() if not k.startswith("_")}

    cid_map = build_circuit_id_map(data, desc_to_provider, existing)
    out = {"_comment": "message"}

    for dev_name in sorted(data["devices"].keys()):
        ifaces = data["devices"][dev_name]
        if not isinstance(ifaces, list):
            continue
        dev_existing = existing.get(dev_name, {})
        dev_out = {}
        for iface in ifaces:
            name = (iface.get("name") or "").strip()
            if not name:
                continue
            if not is_uplink(iface):
                continue
            desc = (iface.get("description") or "").strip()
            provider = desc_to_provider.get(desc, desc or "")
            if provider and provider == desc and len(desc) > 30:
                provider = desc[:27] + "..."
            entry = dev_existing.get(name)
            cid = cid_map.get((dev_name, name), "")
            #
            rate_gbps = None
            if entry and isinstance(entry, dict):
                rate_gbps = entry.get("commit_rate_gbps")
                if rate_gbps is None and entry.get("commit_rate_kbps") is not None:
                    v = entry["commit_rate_kbps"]
                    rate_gbps = (v / 1_000_000.0) if v >= 1000 else v
                #
                #
                merged_entry = dict(entry)
                merged_entry["provider"] = entry.get("provider", provider)
                merged_entry["circuit_id"] = cid
                merged_entry["commit_rate_gbps"] = rate_gbps
                dev_out[name] = merged_entry
            else:
                dev_out[name] = {
                    "provider": provider,
                    "circuit_id": cid,
                    "commit_rate_gbps": None,
                }
        if dev_out:
            out[dev_name] = dev_out

    if provider_limits is not None:
        out["_provider_limits"] = provider_limits  #
    if provider_sla is not None:
        out["_provider_sla"] = provider_sla  #
    for k, v in existing_meta.items():
        out[k] = v

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    n_dev = sum(1 for k in out if not k.startswith("_"))
    n_links = sum(len(v) for k, v in out.items() if not k.startswith("_") and isinstance(v, dict))
    print("message".format(args.output, n_dev, n_links))


if __name__ == "__main__":
    main()
