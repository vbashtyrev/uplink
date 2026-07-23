"""Zabbix JSON-RPC client, host/item cache, and interface name helpers."""

import json
import os
import re
import sys

ZABBIX_CACHE_FILE = "zabbix_uplinks_cache.json"
BITS_RECEIVED_NAME = "Bits received"
BITS_SENT_NAME = "Bits sent"


def load_zabbix_cache(path):
    """
    Load cache from file. Return (host_id_by_name, items_by_host_iface) or (None, None) on error/missing.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict) or "host_id_by_name" not in data or "items_by_host_iface" not in data:
        return None, None
    host_id_by_name = data["host_id_by_name"]
    items_list = data.get("items_by_host_iface")
    items_by_host_iface = {}
    if isinstance(items_list, list):
        for k, rec in items_list:
            items_by_host_iface[(k[0], k[1])] = rec
    elif isinstance(items_list, dict):
        for k, rec in items_list.items():
            parts = k.split("|", 1)
            if len(parts) == 2:
                items_by_host_iface[(parts[0], parts[1])] = rec
    return host_id_by_name, items_by_host_iface


def save_zabbix_cache(path, host_id_by_name, items_by_host_iface):
    """Save host_id_by_name and items_by_host_iface in JSON (item keys are lists for tuple)."""
    items_list = [[list(pair), rec] for pair, rec in items_by_host_iface.items()]
    data = {"host_id_by_name": host_id_by_name, "items_by_host_iface": items_list}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)


def zabbix_request(url, token, method, params=None, debug=False):
    """
    Call Zabbix API 7 (JSON-RPC 2.0). Authorization: Bearer <token>.
    Return (result, None) or (None, error_msg).
    """
    try:
        import requests
    except ImportError:
        return None, "--zabbix requires the requests module (pip install requests)"
    if params is None:
        params = {}
    if debug:
        print("Zabbix API: {} {}".format(method, params), file=sys.stderr)
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }
    if debug and method in ("map.create", "map.update"):
        print("--- request body (JSON) ---", file=sys.stderr)
        print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)
        print("--- end ---", file=sys.stderr)
    headers = {
        "Content-Type": "application/json-rpc",
        "Authorization": "Bearer {}".format(token),
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return None, "query to Zabbix: {}".format(e)
    if "error" in data:
        err = data["error"]
        return None, "Zabbix API: {} ({})".format(
            err.get("data", err.get("message", "unknown")),
            err.get("code", ""),
        )
    if debug and data.get("result") is not None:
        res = data["result"]
        if isinstance(res, list):
            print(" -> {} records".format(len(res)), file=sys.stderr)
        else:
            print("  -> ok", file=sys.stderr)
    return data.get("result"), None


def validate_zabbix_token(url, token, debug=False):
    """Check token: call user.get with Bearer (Zabbix 7)."""
    result, err = zabbix_request(url, token, "user.get", {"limit": 1}, debug=debug)
    if err:
        return False, err
    return True, None


def get_zabbix_url_token():
    """Get ZABBIX_URL and ZABBIX_TOKEN from the environment, normalize the URL."""
    url = os.environ.get("ZABBIX_URL", "").rstrip("/")
    token = os.environ.get("ZABBIX_TOKEN", "")
    if not url or not token:
        return None, None
    if not url.endswith("/api_jsonrpc.php") and not url.endswith("api_jsonrpc.php"):
        url = url.rstrip("/") + "/api_jsonrpc.php"
    return url, token


def interface_from_key(key):
    """From a key like net.if.in[Ethernet51/1] extract the interface name."""
    if not key:
        return None
    m = re.search(r"\[([^]]+)\]", key)
    if not m:
        return None
    iface = m.group(1).strip().strip('"\'')
    return iface if iface else None


def interface_from_item_name(name):
    """From item name like 'Interface Ethernet51/1(Uplink: ...): Bits received', extract Ethernet51/1."""
    if not name:
        return None
    m = re.search(r"Interface\s+([^\s(:(]+)", name, re.IGNORECASE)
    return m.group(1).strip() if m else None


def normalize_interface_name(name):
    """Normalize interface name for comparison (Ethernet51/1 and ethernet51/1)."""
    if not name:
        return ""
    return name.strip().lower()


# Backward-compatible aliases used across scripts and tests.
_interface_from_key = interface_from_key
_interface_from_item_name = interface_from_item_name
_normalize_interface_name = normalize_interface_name
_get_zabbix_url_token = get_zabbix_url_token


def fetch_zabbix_hosts_and_items(url, token, hostnames, debug=False):
    """
    Find hosts by name in Zabbix and collect items “Bits received” / “Bits sent”.
    Return (host_id_by_name, items_by_host_interface, error).
    """
    valid, err = validate_zabbix_token(url, token, debug=debug)
    if not valid:
        return None, None, err

    result, err = zabbix_request(url, token, "host.get", {
        "output": ["hostid", "host", "name"],
        "filter": {"host": list(hostnames)},
    }, debug=debug)
    if err:
        return None, None, err
    host_id_by_name = {}
    for h in result:
        host_id_by_name[h["host"]] = h["hostid"]
    missing = hostnames - set(host_id_by_name.keys())
    if missing:
        result2, err2 = zabbix_request(url, token, "host.get", {
            "output": ["hostid", "host", "name"],
            "filter": {"name": list(missing)},
        }, debug=debug)
        if not err2 and result2:
            for h in result2:
                host_id_by_name[h["name"]] = h["hostid"]
        missing = hostnames - set(host_id_by_name.keys())
    if missing:
        return None, None, "hosts not found in Zabbix: {}".format(", ".join(sorted(missing)))

    hostids = list(host_id_by_name.values())
    all_items = []
    for search_name in (BITS_RECEIVED_NAME, BITS_SENT_NAME):
        result, err = zabbix_request(url, token, "item.get", {
            "output": ["itemid", "hostid", "name", "key_"],
            "hostids": hostids,
            "search": {"name": search_name},
        }, debug=debug)
        if err:
            return None, None, err
        all_items.extend(result)

    host_by_id = {str(v): k for k, v in host_id_by_name.items()}

    def _item_key(it):
        return it.get("key_") or it.get("key", "")

    items_by_host_iface = {}
    debug_samples = []
    skipped_no_host = 0
    skipped_no_iface = 0
    for item in all_items:
        hostid = item.get("hostid")
        hostname = host_by_id.get(str(hostid)) if hostid is not None else None
        if not hostname:
            skipped_no_host += 1
            continue
        key_str = _item_key(item)
        name = item.get("name", "")
        iface_from_k = interface_from_key(key_str)
        iface_from_n = interface_from_item_name(name)
        iface = iface_from_n or iface_from_k
        if not iface:
            skipped_no_iface += 1
            if debug and len(debug_samples) < 5:
                debug_samples.append({
                    "hostid": hostid, "hostname": hostname, "name": name[:80],
                    "key": key_str[:60] if key_str else "", "from_key": iface_from_k, "from_name": iface_from_n,
                })
            continue
        key_norm = normalize_interface_name(iface)
        if debug and len(debug_samples) < 5 and (hostname, key_norm) not in items_by_host_iface:
            debug_samples.append({
                "hostid": hostid, "hostname": hostname, "name": name[:80],
                "key": key_str[:60] if key_str else "", "iface": iface, "key_norm": key_norm,
            })
        if (hostname, key_norm) not in items_by_host_iface:
            items_by_host_iface[(hostname, key_norm)] = {
                "bits_in": "", "bits_out": "", "itemid_in": "", "itemid_out": "",
            }
        itemid = item.get("itemid")
        if BITS_RECEIVED_NAME in name or name == BITS_RECEIVED_NAME:
            items_by_host_iface[(hostname, key_norm)]["bits_in"] = key_str
            if itemid is not None:
                items_by_host_iface[(hostname, key_norm)]["itemid_in"] = str(itemid)
        if BITS_SENT_NAME in name or name == BITS_SENT_NAME:
            items_by_host_iface[(hostname, key_norm)]["bits_out"] = key_str
            if itemid is not None:
                items_by_host_iface[(hostname, key_norm)]["itemid_out"] = str(itemid)

    if debug:
        if all_items:
            raw = all_items[0]
            print("DEBUG: one raw item from the API (keys): {}".format(list(raw.keys())), file=sys.stderr)
            print("DEBUG:   name={!r} key_={!r} key={!r} hostid={!r}".format(
                raw.get("name"), raw.get("key_"), raw.get("key"), raw.get("hostid")), file=sys.stderr)
        print("DEBUG: skipped_no_host={} skipped_no_iface={}".format(skipped_no_host, skipped_no_iface), file=sys.stderr)
        print("DEBUG: items_by_host_iface: {} pars (hostname, interface)".format(len(items_by_host_iface)), file=sys.stderr)
        for i, (hn, kn) in enumerate(sorted(items_by_host_iface.keys())[:15]):
            rec = items_by_host_iface[(hn, kn)]
            print("  [{}] ({!r}, {!r}) -> in={!r} out={!r}".format(
                i, hn, kn, rec.get("bits_in", "")[:50], rec.get("bits_out", "")[:50]), file=sys.stderr)
        for i, s in enumerate(debug_samples):
            print("DEBUG: sample item {}: hostname={!r} name={!r} key={!r} from_key={!r} from_name={!r} iface={!r} key_norm={!r}".format(
                i, s.get("hostname"), s.get("name"), s.get("key"), s.get("from_key"), s.get("from_name"), s.get("iface"), s.get("key_norm")), file=sys.stderr)

    return host_id_by_name, items_by_host_iface, None
