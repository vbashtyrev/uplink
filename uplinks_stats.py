#!/usr/bin/env python3
"""Collect and report uplink interface data (Arista, Juniper) from NetBox and SSH."""

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

import paramiko
import pynetbox

from env_urls import load_env_file_if_present
from uplinks_config import UPLINK_VRF_NAME

load_env_file_if_present()


def _format_ssh_connect_error(host, e):
    """Format SSH connection error with exception type and errno."""
    exc_type = type(e).__name__
    msg = (str(e).strip() if e else "") or "(no message)"
    line = "SSH: error connecting to {!r}: {} - {}". format(host, exc_type, msg)
    if isinstance(e, OSError) and getattr(e, "errno", None) is not None:
        line += " (errno {})".format(e.errno)
    return line


def _load_ssh_config():
    """Load ~/.ssh/config and return SSHConfig or None."""
    path = os.path.expanduser("~/.ssh/config")
    if not os.path.isfile(path):
        return None
    try:
        config = paramiko.SSHConfig()
        with open(path) as f:
            config.parse(f)
        return config
    except Exception:
        return None


def _resolve_ssh_host(ssh_config, device_name, ssh_host, username):
    """Resolve final (host, user) using SSH config and defaults."""
    if not ssh_config:
        return ssh_host, username
    for alias in (device_name, ssh_host):
        try:
            entry = ssh_config.lookup(alias)
            host = entry.get("hostname")
            if host:
                return host, entry.get("user") or username
        except Exception:
            continue
    return ssh_host, username


def extract_json(text):
    """Extract first valid JSON object from noisy CLI output."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, c in enumerate(text[start:], start):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _juniper_iface_name_desc(iface_dict):
    """Return (name, description) from Junos interface dict."""
    name_list = iface_dict.get("name") or [{}]
    desc_list = iface_dict.get("description") or [{}]
    name = (name_list[0].get("data") or "").strip()
    desc = (desc_list[0].get("data") or "").strip()
    return name, desc


def _juniper_iface_oper_status(iface_dict):
    """Return oper-status (up/down) from Junos interface dict."""
    return _juniper_data(iface_dict.get("oper-status"))


def _juniper_data(field):
    """Extract scalar value from Junos field list with `data`."""
    if not field:
        return None
    if isinstance(field, list) and field and isinstance(field[0], dict):
        val = field[0].get("data")
        if val is not None:
            s = str(val).strip()
            return s if s else None
    return None


def _juniper_speed_to_bps(speed_str):
    """Convert Junos speed string (1000mbps, 10gbps, ...) to bps."""
    if not speed_str:
        return None
    s = str(speed_str).strip().lower().replace(" ", "")
    try:
        if s.endswith("gbps"):
            return int(float(s[:-4]) * 1e9)
        if s.endswith("mbps"):
            return int(float(s[:-4]) * 1e6)
        if s.endswith("kbps"):
            return int(float(s[:-4]) * 1e3)
        if s.endswith("bps") or s.isdigit():
            return int(float(s.replace("bps", "") or s))
    except (ValueError, TypeError):
        pass
    return None


def _juniper_uplink_is_unit0(name):
    """Return True for physical or *.0 units (others treated as VLANs)."""
    if not name:
        return False
    if "." not in name:
        return True
    return name.split(".")[-1] == "0"


def parse_juniper_uplinks(json_data, require_link_up=False):
    """Return list of (name, description) Junos uplinks with 'Uplink:' in description."""
    out = []
    infos = json_data.get("interface-information") or []
    if isinstance(infos, dict):
        infos = [infos]
    for info in infos:
        for ph in info.get("physical-interface") or []:
            name, desc = _juniper_iface_name_desc(ph)
            if name and "Uplink:" in desc:
                if require_link_up:
                    oper = _juniper_iface_oper_status(ph)
                    if oper is not None and oper.lower() != "up":
                        continue
                if not _juniper_uplink_is_unit0(name):
                    continue
                out.append((name, desc))
        for log in info.get("logical-interface") or []:
            name, desc = _juniper_iface_name_desc(log)
            if name and "Uplink:" in desc:
                if require_link_up:
                    oper = _juniper_iface_oper_status(log)
                    if oper is not None and oper.lower() != "up":
                        continue
                if not _juniper_uplink_is_unit0(name):
                    continue
                out.append((name, desc))
    return out


def _extract_xml_interface_information(text):
    """Extract first <interface-information>...</interface-information> block from XML output."""
    blocks = _extract_all_xml_interface_information_blocks(text)
    return blocks[0] if blocks else None


def _extract_all_xml_interface_information_blocks(text):
    """Extract all <interface-information>...</interface-information> blocks from XML output."""
    blocks = []
    start_tag = "<interface-information"
    pos = 0
    while True:
        pos = text.find(start_tag, pos)
        if pos == -1:
            break
        depth = 0
        i = pos
        while i < len(text):
            if text[i] == "<":
                if i + 1 < len(text) and text[i + 1] == "/":
                    depth -= 1
                    if depth == 0:
                        end = text.find(">", i)
                        if end != -1:
                            blocks.append(text[pos : end + 1])
                        pos = end + 1 if end != -1 else len(text)
                        break
                elif i + 1 < len(text) and not text[i + 1 : i + 2].isspace():
                    depth += 1
            i += 1
        else:
            pos += 1
    return blocks


def _parse_junos_rpc_reply_and_find_interface_information(xml_text):
    """Parse full <rpc-reply> XML and return list of interface-information elements."""
    start = xml_text.find("<rpc-reply")
    if start == -1:
        return []
    end = xml_text.find("</rpc-reply>")
    if end == -1:
        return []
    doc = xml_text[start : end + len("</rpc-reply>")]
    try:
        root = ET.fromstring(doc)
    except ET.ParseError:
        return []
    out = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "interface-information":
            out.append(elem)
    return out


def _juniper_xml_elem_text(elem):
    """Return text value from Junos XML element (including nested <data>)."""
    if elem is None:
        return ""
    for child in elem:
        if child.tag.split("}")[-1] == "data" and child.text:
            return (child.text or "").strip()
    return (elem.text or "").strip()


def _juniper_xml_child(elem, local_name):
    """Find child XML element by local tag name (without namespace)."""
    if elem is None:
        return None
    for c in elem:
        if c.tag.split("}")[-1] == local_name:
            return c
    return None


def _juniper_xml_iface_name_desc_oper(elem):
    """From the physical-interface or logical-interface (XML) element, extract (name, description, oper_status)."""
    name_el = _juniper_xml_child(elem, "name")
    desc_el = _juniper_xml_child(elem, "description")
    oper_el = _juniper_xml_child(elem, "oper-status")
    name = _juniper_xml_elem_text(name_el) if name_el is not None else ""
    desc = _juniper_xml_elem_text(desc_el) if desc_el is not None else ""
    oper = _juniper_xml_elem_text(oper_el) if oper_el is not None else None
    return name, desc, oper


def parse_juniper_uplinks_from_xml(xml_root, require_link_up=False, debug_cb=None):
    """
    From the XML root (interface-information) extract all interfaces with 'Uplink:' into description.
    XML does not lose duplicate tags (unlike JSON). We traverse all nodes (including nested logical-interfaces).
    Return: [(name, desc), ...]
    debug_cb(msg) - when debugging, it is called for each interface under consideration.
    """
    out = []
    for elem in xml_root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag not in ("physical-interface", "logical-interface"):
            continue
        name, desc, oper = _juniper_xml_iface_name_desc_oper(elem)
        if debug_cb:
            debug_cb("  elem: tag={} name={!r} desc={!r} oper={!r}".format(tag, name, desc, oper))
        if not name or "Uplink:" not in desc:
            if debug_cb and name:
                debug_cb(" -> skip: no Uplink in desc")
            continue
        if require_link_up and oper is not None and oper.lower() != "up":
            if debug_cb:
                debug_cb(" -> skip: oper not up")
            continue
        if not _juniper_uplink_is_unit0(name):
            if debug_cb:
                debug_cb(" -> skip: not unit 0")
            continue
        if debug_cb:
            debug_cb(" -> added")
        out.append((name, desc))
    return out


def parse_juniper_descriptions_all(json_data):
    """
    From Juniper JSON (show interfaces descriptions) extract all interfaces.
    Return: [(name, description, oper_status), ...]; oper_status can be None.
    """
    out = []
    infos = json_data.get("interface-information") or []
    if isinstance(infos, dict):
        infos = [infos]
    for info in infos:
        for ph in info.get("physical-interface") or []:
            name, desc = _juniper_iface_name_desc(ph)
            if name:
                oper = _juniper_iface_oper_status(ph)
                out.append((name, desc, oper))
        for log in info.get("logical-interface") or []:
            name, desc = _juniper_iface_name_desc(log)
            if name:
                oper = _juniper_iface_oper_status(log)
                out.append((name, desc, oper))
    return out


def parse_arista_uplinks(json_data):
    """From Arista JSON, extract interfaces with 'Uplink:' into description."""
    out = []
    descs = json_data.get("interfaceDescriptions") or {}
    for name, obj in descs.items():
        if not isinstance(obj, dict):
            continue
        desc = (obj.get("description") or "").strip()
        if "Uplink:" in desc:
            out.append((name, desc))
    return out


def _arista_interface_link_up(if_obj):
    """True, if according to show interfaces the interface is considered to be up (link up)."""
    if not if_obj:
        return False
    line_proto = (if_obj.get("lineProtocolStatus") or "").strip().lower()
    iface_status = (if_obj.get("interfaceStatus") or "").strip().lower()
    if line_proto == "down":
        return False
    if iface_status in ("disabled", "notconnect", "down"):
        return False
    return True


def _is_global_routable_address(addr_with_prefix):
    """
    Globally routed addresses only (IPv4 and IPv6).
    We exclude: private (10/8, 172.16/12, 192.168/16), link-local (169.254/16, fe80::/10),
    unique local (fc00::/7), loopback (127/8, ::1).
    """
    if not addr_with_prefix or not isinstance(addr_with_prefix, str):
        return False
    s = addr_with_prefix.strip().split("/")[0].lower()
    if ":" in s:
        # IPv6: exclude fe80::/10, fc00::/7, ::1
        if s == "::1" or s == "0:0:0:0:0:0:0:1":
            return False
        if s.startswith("fe8") or s.startswith("fe9") or s.startswith("fea") or s.startswith("feb"):
            return False
        if s.startswith("fc") or s.startswith("fd"):
            return False
        return True
    # IPv4: exclude 10/8, 172.16/12, 192.168/16, 169.254/16, 127/8
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b, c, d = (int(x) for x in parts)
    except ValueError:
        return False
    if a == 10:
        return False
    if a == 172 and 16 <= b <= 31:
        return False
    if a == 192 and b == 168:
        return False
    if a == 169 and b == 254:
        return False
    if a == 127:
        return False
    return True


def _parse_arista_interface_ips(if_obj):
    """
    From Arista show interfaces, pull out IPv4/IPv6 for the routed interface.
    Only global routable addresses (we do not take private/link-local).
    Return: {"ipv4_addresses": ["addr/prefix", ...], "ipv6_addresses": ["addr/prefix", ...]}.
    """
    ipv4 = []
    ipv6 = []
    if (if_obj.get("forwardingModel") or "").strip().lower() != "routed":
        return {"ipv4_addresses": ipv4, "ipv6_addresses": ipv6}
    # IPv4: interfaceAddress - array, element: primaryIp: { address, maskLen }
    for block in (if_obj.get("interfaceAddress") or []):
        if not isinstance(block, dict):
            continue
        pi = block.get("primaryIp")
        if not isinstance(pi, dict):
            continue
        addr = (pi.get("address") or "").strip()
        if not addr or addr == "0.0.0.0":
            continue
        mask = pi.get("maskLen")
        addr_str = "{}/{}".format(addr, mask) if mask is not None else addr
        if not _is_global_routable_address(addr_str):
            continue
        ipv4.append(addr_str)
    # IPv6: globalUnicastIp6s only, global only (not link-local, not unique local)
    ip6_block = if_obj.get("interfaceAddressIp6")
    if isinstance(ip6_block, dict):
        for g in (ip6_block.get("globalUnicastIp6s") or []):
            if not isinstance(g, dict):
                continue
            addr = (g.get("address") or "").strip()
            if not addr:
                continue
            subnet = (g.get("subnet") or "").strip()
            prefix = ""
            if "/" in subnet:
                prefix = subnet.split("/", 1)[1].strip()
            addr_str = "{}/{}".format(addr, prefix) if prefix else addr
            if not _is_global_routable_address(addr_str):
                continue
            ipv6.append(addr_str)
    return {"ipv4_addresses": ipv4, "ipv6_addresses": ipv6}


def arista_cli_interface_name(name):
    """Ethernet72/1 -> ethernet 72/1 for the show int ..."""
    return re.sub(r"([a-zA-Z]+)(\d)", r"\1 \2", name).strip().lower()


def is_juniper_platform(platform_name):
    """By platform name from NetBox: JunOS / Juniper → True."""
    if not platform_name:
        return False
    n = platform_name.lower()
    return "junos" in n or "juniper" in n


def is_arista_platform(platform_name):
    """By platform name from NetBox: Arista EOS → True."""
    if not platform_name:
        return False
    n = platform_name.lower()
    return "arista" in n or "eos" in n


def get_device_platform_name(device, nb):
    """Platform name from NetBox (device.platform.name) to identify Juniper/Arista."""
    pl = getattr(device, "platform", None)
    if pl is None:
        return None
    if hasattr(pl, "name"):
        return getattr(pl, "name", None)
    if isinstance(pl, int):
        p = nb.dcim.platforms.get(pl)
        return getattr(p, "name", None) if p else None
    return None


def get_ssh_uplinks(
    host,
    username,
    password,
    netbox_interface_names=None,
    platform_name=None,
    timeout=45,
    command_timeout=120,
    log=None,
    debug_json=False,
):
    """
    Connect via SSH, execute the command and return a list (interface, description) with 'Uplink:'.
    Device type: platform_name (NetBox) or by banner (JUNOS). log - callback, debug_json - JSON output.
    """
    def _log(msg):
        if log:
            log(msg)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        _log("SSH: connecting to {}...".format(host))
        client.connect(
            host,
            username=username,
            password=password,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        _log("SSH: connected")
    except (socket.timeout, paramiko.SSHException, OSError) as e:
        _log(_format_ssh_connect_error(host, e))
        return None, str(e)

    channel = client.invoke_shell(width=256)
    channel.settimeout(15)

    def read_until(patterns, max_wait=30):
        buf = []
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            if channel.recv_ready():
                chunk = channel.recv(65536).decode("utf-8", errors="replace")
                buf.append(chunk)
                text = "".join(buf)
                for p in patterns:
                    if p in text:
                        return text
            else:
                time.sleep(0.2)
        return "".join(buf)

    def read_until_json_and_prompt(max_wait=120):
        buf = []
        deadline = time.monotonic() + max_wait
        last_data = time.monotonic()
        while time.monotonic() < deadline:
            if channel.recv_ready():
                chunk = channel.recv(65536).decode("utf-8", errors="replace")
                buf.append(chunk)
                last_data = time.monotonic()
            else:
                time.sleep(0.15)
            text = "".join(buf)
            if "#" in text or ">" in text:
                data = extract_json(text)
                if data is not None:
                    return text
            if time.monotonic() - last_data > 3 and buf:
                return text
        return "".join(buf)

    send = channel.send

    _ = read_until([">", "#", ":", "login", "Login", "Password", "password"], max_wait=20)
    send(username + "\r\n")
    time.sleep(0.5)
    out_after_user = read_until([">", "#", "password", "Password", "login", "Login"], max_wait=20)
    if "password" in out_after_user.lower() or "Password" in out_after_user:
        send(password + "\r\n")
        time.sleep(0.8)
    out_after_pass = read_until([">", "#", "login", "Login"], max_wait=25)

    if platform_name is not None:
        is_juniper = is_juniper_platform(platform_name)
        if not is_juniper and not is_arista_platform(platform_name):
            _log("SSH: platform '{}' - we consider Arista".format(platform_name))
    else:
        is_juniper = "JUNOS" in out_after_pass
    _log("SSH: defined as {}".format("Juniper" if is_juniper else "Arista"))

    uplinks = []
    if not is_juniper and netbox_interface_names:
        for iface_name in netbox_interface_names:
            cli_name = arista_cli_interface_name(iface_name)
            cmd = f"show int {cli_name} description | json\r\n"
            send(cmd)
            output = read_until_json_and_prompt(max_wait=command_timeout)
            data = extract_json(output)
            if debug_json and (data is not None or output):
                if data is not None:
                    _log("--- SSH JSON ({}): ---\n{}".format(iface_name, json.dumps(data, indent=2, ensure_ascii=False)))
                else:
                    _log("--- SSH JSON not extracted for {} (up to 3000 characters) ---\n{}".format(iface_name, (output[:3000] if output else "(empty)")))
            if data:
                descs = data.get("interfaceDescriptions") or {}
                obj = descs.get(iface_name)
                if not obj and descs:
                    obj = next(iter(descs.values()), None)
                desc = (obj.get("description") or "").strip() if isinstance(obj, dict) else ""
                if "Uplink:" in desc:
                    uplinks.append((iface_name, desc))
            time.sleep(0.3)
    else:
        if is_juniper:
            cmd = "show interfaces descriptions | display json | no-more\r\n"
        else:
            cmd = "show interfaces description | json | no-more\r\n"
        send(cmd)
        output = read_until_json_and_prompt(max_wait=command_timeout)
        data = extract_json(output)
        if debug_json:
            if data is not None:
                _log("--- SSH JSON ---\n" + json.dumps(data, indent=2, ensure_ascii=False))
            else:
                _log("--- SSH JSON not extracted. Raw output (up to 6000 characters) ---\n" + (output[:6000] if output else "(empty)"))
        if not data:
            client.close()
            _log("SSH: Failed to extract JSON from output")
            return None, "failed to extract JSON from output"
        if is_juniper:
            uplinks = parse_juniper_uplinks(data)
        else:
            uplinks = parse_arista_uplinks(data)

    client.close()
    _log("SSH: ready ({} uplinks)".format(len(uplinks)))
    return sorted(uplinks, key=lambda x: x[0]), None


def format_cell(lines, not_found_comment):
    """Format the list (name, desc) into cell text or comment."""
    if not lines:
        return not_found_comment
    return "\n".join(f"{name}: {desc}" for name, desc in lines)


def process_one_device(
    device,
    nb,
    ssh_user,
    ssh_pass,
    ssh_suffix,
    netbox_not_found,
    ssh_not_found,
    progress_print,
):
    """Process one device: NetBox + SSH. Returns (name, ip, netbox_cell, ssh_cell)."""
    progress_print(device.name, "NetBox: getting interfaces...")
    primary_ip = getattr(device, "primary_ip4", None) or getattr(device, "primary_ip", None)
    if primary_ip:
        if isinstance(primary_ip, int):
            ip_obj = nb.ipam.ip_addresses.get(primary_ip)
            ip_display = getattr(ip_obj, "address", None) or str(primary_ip)
        else:
            ip_display = getattr(primary_ip, "address", None) or str(primary_ip)
    else:
        ip_display = ""

    netbox_uplinks = []
    for iface in nb.dcim.interfaces.filter(device_id=device.id):
        desc = (iface.description or "").strip()
        if "Uplink:" in desc:
            netbox_uplinks.append((iface.name, desc))
    netbox_uplinks = sorted(netbox_uplinks, key=lambda x: x[0])
    netbox_cell = format_cell(netbox_uplinks, netbox_not_found)
    progress_print(device.name, "NetBox: {} uplinks".format(len(netbox_uplinks)))

    ssh_host = device.name + ssh_suffix
    netbox_names = [n for n, _ in netbox_uplinks]
    platform_name = get_device_platform_name(device, nb)
    debug_json = os.environ.get("DEBUG_SSH_JSON", "").lower() in ("1", "true", "yes")
    log_cb = lambda msg: progress_print(device.name, msg)
    uplinks, err = get_ssh_uplinks(
        ssh_host,
        ssh_user,
        ssh_pass,
        netbox_interface_names=netbox_names if netbox_names else None,
        platform_name=platform_name,
        log=log_cb,
        debug_json=debug_json,
    )
    if err:
        ssh_cell = "{} ({})".format(ssh_not_found, err)
    elif uplinks is not None:
        ssh_cell = format_cell(uplinks, ssh_not_found)
    else:
        ssh_cell = ssh_not_found

    return (device.name, ip_display, netbox_cell, ssh_cell)


# --- Arista statistics mode: read_until by channel, return data ---
def read_until(channel, patterns, max_wait=30):
    buf = []
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        if channel.recv_ready():
            chunk = channel.recv(65536).decode("utf-8", errors="replace")
            buf.append(chunk)
            text = "".join(buf)
            for p in patterns:
                if p in text:
                    return text
        else:
            time.sleep(0.2)
    return "".join(buf)


def read_until_json_and_prompt(channel, timeout=120):
    """Read the output before the prompt and extract the JSON."""
    buf = []
    deadline = time.monotonic() + timeout
    last_data = time.monotonic()
    while time.monotonic() < deadline:
        if channel.recv_ready():
            chunk = channel.recv(65536).decode("utf-8", errors="replace")
            buf.append(chunk)
            last_data = time.monotonic()
        else:
            time.sleep(0.15)
        text = "".join(buf)
        if "#" in text or ">" in text:
            data = extract_json(text)
            if data is not None:
                return data
        if time.monotonic() - last_data > 3 and buf:
            break
    return extract_json("".join(buf))


def _looks_like_cli_prompt(text):
    """Check that there is a CLI prompt at the end of the buffer (user@host> or host#), and not just a '>' from XML/JSON."""
    if not text or not text.strip():
        return False
    last_line = text.split("\n")[-1].strip() if "\n" in text else text.strip()
    if not last_line:
        return False
    if not (last_line.endswith(">") or last_line.endswith("#")):
        return False
    return "@" in last_line


def read_until_prompt(channel, timeout=120):
    """Read output until the CLI prompt at the end (user@host> or host#), return raw text. Do not return to the first '>' in XML/JSON."""
    buf = []
    deadline = time.monotonic() + timeout
    last_data = time.monotonic()
    while time.monotonic() < deadline:
        if channel.recv_ready():
            chunk = channel.recv(65536).decode("utf-8", errors="replace")
            buf.append(chunk)
            last_data = time.monotonic()
        else:
            time.sleep(0.15)
        text = "".join(buf)
        if _looks_like_cli_prompt(text):
            return text
        if time.monotonic() - last_data > 3 and buf:
            break
    return "".join(buf)


def get_arista_uplink_stats(host, username, password, timeout=45, command_timeout=90, log=None):
    """
    SSH to Arista: list of interfaces with "Uplink:", for each show interfaces + transceiver,
    when bridged - switchport configuration source. Return: dict list or (None, error).
    """
    def _log(msg):
        if log:
            log(msg)

    start_time = time.monotonic()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        _log("SSH: connecting to {}...".format(host))
        client.connect(
            host,
            username=username,
            password=password,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        _log("SSH: connected")
    except (socket.timeout, paramiko.SSHException, OSError) as e:
        elapsed = time.monotonic() - start_time
        _log(_format_ssh_connect_error(host, e))
        _log("SSH: {:.0f} seconds have elapsed since the attempt began.".format(elapsed))
        return None, "{} (in {:.0f} s)".format(str(e), elapsed)

    channel = client.invoke_shell(width=256)
    channel.settimeout(15)
    send = channel.send

    _ = read_until(channel, [">", "#", ":", "login", "Login", "Password", "password"], max_wait=20)
    send(username + "\r\n")
    time.sleep(0.5)
    out_after_user = read_until(channel, [">", "#", "password", "Password", "login", "Login"], max_wait=20)
    if "password" in out_after_user.lower() or "Password" in out_after_user:
        send(password + "\r\n")
        time.sleep(0.8)
    read_until(channel, [">", "#"], max_wait=25)

    send("show interfaces description | json | no-more\r\n")
    desc_data = read_until_json_and_prompt(channel, timeout=command_timeout)
    if not desc_data:
        elapsed = time.monotonic() - start_time
        client.close()
        _log("SSH: failed to get show interfaces description | json | no-more")
        _log("SSH: {:.0f} seconds have elapsed since the attempt began.".format(elapsed))
        return None, "failed to get show interfaces description | json (via {:.0f} s)". format(elapsed)

    uplinks = parse_arista_uplinks(desc_data)
    if not uplinks:
        client.close()
        _log("SSH: no uplink interfaces found")
        return [], None

    # List of interfaces in VRF internet (show vrf internet | json → vrfs.internet.interfaces)
    internet_interfaces = set()
    send("show vrf {} | json | no-more\r\n".format(UPLINK_VRF_NAME))
    time.sleep(0.2)
    vrf_data = read_until_json_and_prompt(channel, timeout=command_timeout)
    if vrf_data:
        vrf_internet = (vrf_data.get("vrfs") or {}).get(UPLINK_VRF_NAME)
        if isinstance(vrf_internet, dict):
            for iface in (vrf_internet.get("interfaces") or []):
                if iface:
                    internet_interfaces.add(iface)
    _log("SSH: found uplink interfaces: {} (in the report only with link up)". format(len(uplinks)))
    result = []

    for iface_name, desc in uplinks:
        cli_name = arista_cli_interface_name(iface_name)
        send("show interfaces {} | json | no-more\r\n".format(cli_name))
        time.sleep(0.2)
        if_data = read_until_json_and_prompt(channel, timeout=command_timeout)
        send("show interfaces {} transceiver | json | no-more\r\n".format(cli_name))
        time.sleep(0.2)
        trans_data = read_until_json_and_prompt(channel, timeout=command_timeout)

        if_data = (if_data or {}).get("interfaces") or {}
        trans_data = (trans_data or {}).get("interfaces") or {}
        if_obj = if_data.get(iface_name) or {}
        trans_obj = trans_data.get(iface_name) or {}

        if not _arista_interface_link_up(if_obj):
            time.sleep(0.2)
            continue

        switchport_config = None
        if (if_obj.get("forwardingModel") or "").strip().lower() == "bridged":
            send("show interfaces {} switchport configuration source | json | no-more\r\n".format(cli_name))
            time.sleep(0.2)
            sw_data = read_until_json_and_prompt(channel, timeout=command_timeout)
            sw_interfaces = (sw_data or {}).get("interfaces") or {}
            sw_iface = sw_interfaces.get(iface_name) or {}
            if sw_iface:
                switchport_config = sw_iface
            time.sleep(0.2)

        row = {
            "name": if_obj.get("name") or iface_name,
            "mediaType": trans_obj.get("mediaType"),
            "bandwidth": if_obj.get("bandwidth"),
            "duplex": if_obj.get("duplex"),
            "description": if_obj.get("description") or desc,
            "physicalAddress": if_obj.get("physicalAddress"),
            "mtu": if_obj.get("mtu"),
            "txPower": trans_obj.get("txPower"),
            "forwardingModel": if_obj.get("forwardingModel"),
        }
        if switchport_config is not None:
            row["switchportConfiguration"] = switchport_config
        # IP addresses for routed interfaces (check with NetBox in netbox_checks.py)
        ips = _parse_arista_interface_ips(if_obj)
        if ips["ipv4_addresses"] or ips["ipv6_addresses"]:
            row["ipv4_addresses"] = ips["ipv4_addresses"]
            row["ipv6_addresses"] = ips["ipv6_addresses"]
            if_name = (if_obj.get("name") or iface_name or "").strip()
            if if_name and if_name in internet_interfaces:
                row["ip_vrf"] = UPLINK_VRF_NAME
        result.append(row)
        time.sleep(0.3)

    client.close()
    _log("SSH: records collected: {}".format(len(result)))
    return result, None


def _parse_juniper_logical_mtu(log_iface):
    """From the Junos logical-interface, take the MTU from the first address-family with a numeric mtu."""
    afs = log_iface.get("address-family") or []
    if not isinstance(afs, list):
        afs = [afs] if afs else []
    for af in afs:
        mtu_raw = _juniper_data(af.get("mtu"))
        if mtu_raw and str(mtu_raw).isdigit():
            return int(mtu_raw)
    return None


def _parse_juniper_logical_ip_addresses(log_iface):
    """
    From the Junos logical-interface, extract ifa-local for address-family inet (IPv4) and inet6 (IPv6).
    Return: {"ipv4_addresses": ["addr/prefix", ...], "ipv6_addresses": ["addr/prefix", ...]}.
    """
    ipv4 = []
    ipv6 = []
    afs = log_iface.get("address-family") or []
    if not isinstance(afs, list):
        afs = [afs] if afs else []
    for af in afs:
        if not isinstance(af, dict):
            continue
        family = _juniper_data(af.get("address-family-name")) or ""
        if family not in ("inet", "inet6"):
            continue
        addrs = af.get("interface-address") or []
        if not isinstance(addrs, list):
            addrs = [addrs] if addrs else []
        for ia in addrs:
            if not isinstance(ia, dict):
                continue
            ifa_local = _juniper_data(ia.get("ifa-local"))
            ifa_dest = _juniper_data(ia.get("ifa-destination"))
            if not ifa_local:
                continue
            prefix = ""
            if ifa_dest and "/" in str(ifa_dest):
                prefix = str(ifa_dest).split("/", 1)[1]
            addr_str = str(ifa_local).strip() + ("/" + prefix if prefix else "")
            if not _is_global_routable_address(addr_str):
                continue
            if family == "inet":
                ipv4.append(addr_str)
            else:
                ipv6.append(addr_str)
    return {"ipv4_addresses": ipv4, "ipv6_addresses": ipv6}


def _juniper_ae_bundle_name(iface_json):
    """
    From the JSON output show interfaces <name> | display json pull out ae-bundle-name
    (interface in LAG: logical-interface → address-family aenet → ae-bundle-name).
    Return: a string of type "ae5.0" or None.
    """
    infos = iface_json.get("interface-information") or []
    if isinstance(infos, dict):
        infos = [infos]
    for info in infos:
        for ph in info.get("physical-interface") or []:
            logics = ph.get("logical-interface") or []
            if not isinstance(logics, list):
                logics = [logics] if logics else []
            for log in logics:
                afs = log.get("address-family") or []
                if not isinstance(afs, list):
                    afs = [afs] if afs else []
                for af in afs:
                    if _juniper_data(af.get("address-family-name")) == "aenet":
                        return _juniper_data(af.get("ae-bundle-name"))
    return None


def _juniper_lacp_member_names(lacp_json):
    """
    From the JSON output show lacp interfaces <ae> | display json pull names
    physical members of the LAG (lag-lacp-state / lag-lacp-protocol → name).
    Return: list of strings ["et-0/0/3", ...] without duplicates.
    """
    seen = set()
    lists = lacp_json.get("lacp-interface-information-list") or []
    if isinstance(lists, dict):
        lists = [lists]
    for lst in lists:
        blocks = lst.get("lacp-interface-information") or []
        if not isinstance(blocks, list):
            blocks = [blocks] if blocks else []
        for blk in blocks:
            for state in blk.get("lag-lacp-state") or []:
                n = _juniper_data(state.get("name"))
                if n and n not in seen:
                    seen.add(n)
            for proto in blk.get("lag-lacp-protocol") or []:
                n = _juniper_data(proto.get("name"))
                if n and n not in seen:
                    seen.add(n)
    return list(seen)


def _juniper_interface_slot(iface_name):
    """
    From the Junos interface name (et-0/0/3, xe-0/1/0) extract (fpc, pic, port).
    Correspondence: type-fpc/pic/port → FPC fpc, PIC pic, Xcvr port. Return (int,int,int) or None.
    """
    if not iface_name:
        return None
    m = re.match(r"^[a-zA-Z]+-(\d+)/(\d+)/(\d+)$", iface_name.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _juniper_optics_tx_power_dbm(diag_json):
    """
    From JSON show interfaces diagnostics optics <name> | display json pull out
    average laser-output-power-dbm over all lanes (optics-diagnostics-lane-values).
    Return: float (dBm) or None.
    """
    values = []
    infos = diag_json.get("interface-information") or []
    if isinstance(infos, dict):
        infos = [infos]
    for info in infos:
        phys = info.get("physical-interface") or []
        if not isinstance(phys, list):
            phys = [phys] if phys else []
        for ph in phys:
            od = ph.get("optics-diagnostics") or []
            if not isinstance(od, list):
                od = [od] if od else []
            for opt in od:
                lanes = opt.get("optics-diagnostics-lane-values") or []
                if not isinstance(lanes, list):
                    lanes = [lanes] if lanes else []
                for lane in lanes:
                    dbm = _juniper_data(lane.get("laser-output-power-dbm"))
                    if dbm is not None:
                        try:
                            values.append(float(dbm))
                        except (ValueError, TypeError):
                            pass
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _juniper_chassis_media_type(chassis_json, fpc, pic, port):
    """
    From JSON show chassis hardware | display json pull out SFP model (description)
    for FPC fpc, PIC pic, Xcvr port slot. Return string or None.
    """
    invs = chassis_json.get("chassis-inventory") or []
    if isinstance(invs, dict):
        invs = [invs]
    for inv in invs:
        chasses = inv.get("chassis") or []
        if isinstance(chasses, dict):
            chasses = [chasses]
        for ch in chasses:
            modules = ch.get("chassis-module") or []
            if not isinstance(modules, list):
                modules = [modules] if modules else []
            for mod in modules:
                if _juniper_data(mod.get("name")) != "FPC {}".format(fpc):
                    continue
                submods = mod.get("chassis-sub-module") or []
                if not isinstance(submods, list):
                    submods = [submods] if submods else []
                for sub in submods:
                    if _juniper_data(sub.get("name")) != "PIC {}".format(pic):
                        continue
                    xcvrs = sub.get("chassis-sub-sub-module") or []
                    if not isinstance(xcvrs, list):
                        xcvrs = [xcvrs] if xcvrs else []
                    for xc in xcvrs:
                        xcvr_name = _juniper_data(xc.get("name"))
                        if xcvr_name == "Xcvr {}".format(port):
                            return _juniper_data(xc.get("description"))
    return None


def _parse_juniper_phy_iface(ph):
    """Extract fields from physical-interface Junos as dict (same keys as Arista)."""
    name = _juniper_data(ph.get("name"))
    desc = _juniper_data(ph.get("description")) or ""
    speed_str = _juniper_data(ph.get("speed"))
    bandwidth = _juniper_speed_to_bps(speed_str)
    mtu_raw = _juniper_data(ph.get("mtu"))
    mtu = int(mtu_raw) if mtu_raw and str(mtu_raw).isdigit() else None
    mac = _juniper_data(ph.get("current-physical-address"))
    # link-type in Junos: Full-Duplex, Half-Duplex (on 10G/40G/100G Junos often does not output - according to the standard only full)
    duplex_raw = _juniper_data(ph.get("link-type"))
    duplex = None
    if duplex_raw:
        d = str(duplex_raw).lower()
        if "full" in d:
            duplex = "full"
        elif "half" in d:
            duplex = "half"
        else:
            duplex = duplex_raw
    if duplex is None and bandwidth is not None and bandwidth >= 10_000_000_000:
        duplex = "full" # 10G+ full duplex only, half not defined in standards
    return {
        "name": name or "",
        "description": desc,
        "mediaType": None,
        "bandwidth": bandwidth,
        "duplex": duplex,
        "physicalAddress": mac,
        "mtu": mtu,
        "txPower": None,
        "forwardingModel": None,
    }


def get_juniper_uplink_stats(host, username, password, timeout=45, command_timeout=90, log=None):
    """
    SSH to Juniper (Junos): list of interfaces with "Uplink:" in description,
    for each show interfaces <name> detail | display json. Return: dict list (same format as Arista) or (None, error).
    """
    def _log(msg):
        if log:
            log(msg)

    start_time = time.monotonic()
    debug = os.environ.get("DEBUG_JUNIPER_UPLINKS", "").strip().lower() in ("1", "true", "yes")
    def _dbg(msg):
        if debug and log:
            log("[DEBUG] " + msg)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        _log("SSH: connecting to {}...".format(host))
        client.connect(
            host,
            username=username,
            password=password,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        _log("SSH: connected")
    except (socket.timeout, paramiko.SSHException, OSError) as e:
        elapsed = time.monotonic() - start_time
        _log(_format_ssh_connect_error(host, e))
        _log("SSH: {:.0f} seconds have elapsed since the attempt began.".format(elapsed))
        return None, "{} (in {:.0f} s)".format(str(e), elapsed)

    channel = client.invoke_shell(width=256)
    channel.settimeout(15)
    send = channel.send

    _ = read_until(channel, [">", "#", ":", "login", "Login", "Password", "password"], max_wait=20)
    send(username + "\r\n")
    time.sleep(0.5)
    out_after_user = read_until(channel, [">", "#", "password", "Password", "login", "Login"], max_wait=20)
    if "password" in out_after_user.lower() or "Password" in out_after_user:
        send(password + "\r\n")
        time.sleep(0.8)
    read_until(channel, [">", "#"], max_wait=25)

    send("show interfaces descriptions | display json | no-more\r\n")
    desc_data = read_until_json_and_prompt(channel, timeout=command_timeout)
    if not desc_data:
        elapsed = time.monotonic() - start_time
        client.close()
        _log("SSH: failed to get show interfaces descriptions | display json")
        _log("SSH: {:.0f} seconds have elapsed since the attempt began.".format(elapsed))
        return None, "failed to get show interfaces descriptions | display json (via {:.0f} s)". format(elapsed)

    uplinks = parse_juniper_uplinks(desc_data, require_link_up=True)
    _dbg("Step 1 (JSON): parse_juniper_uplinks(require_link_up=True) returned {} records". format(len(uplinks)))
    if not uplinks:
        _log("SSH: no uplinks found in JSON, try display xml (duplicate keys in JSON)...")
        send("show interfaces descriptions | display xml | no-more\r\n")
        time.sleep(0.2)
        xml_text = read_until_prompt(channel, timeout=command_timeout)
        _dbg("Step 2 (XML raw): len(xml_text)={}, _looks_like_cli_prompt={}". format(len(xml_text), _looks_like_cli_prompt(xml_text)))
        _dbg("Step 2: start (300 characters): {!r}".format(xml_text[:300] if len(xml_text) >= 300 else xml_text[:]))
        _dbg("Step 2: end (300 characters): {!r}".format(xml_text[-300:] if len(xml_text) > 300 else ""))
        interface_information_roots = _parse_junos_rpc_reply_and_find_interface_information(xml_text)
        _dbg("Step 3 (rpc-reply parsing): interface-information elements found: {}".format(len(interface_information_roots)))
        for i, root in enumerate(interface_information_roots):
            _dbg("Step 3: root[{}] tag={}".format(i, root.tag))
        for root in interface_information_roots:
            from_block = parse_juniper_uplinks_from_xml(root, require_link_up=True, debug_cb=_dbg if debug else None)
            _dbg("Step 4 (block parsing): parse_juniper_uplinks_from_xml returned {} records: {}".format(len(from_block), from_block))
            uplinks.extend(from_block)
        seen = set()
        uplinks = [(n, d) for n, d in uplinks if n not in seen and not seen.add(n)]
        _dbg("Step 5 (after deduplication): total uplinks: {}".format(len(uplinks)))
        if not uplinks:
            client.close()
            _log("SSH: no uplink interfaces with Link up found")
            return [], None
        time.sleep(0.2)

    send("show chassis hardware | display json | no-more\r\n")
    time.sleep(0.2)
    chassis_hw = read_until_json_and_prompt(channel, timeout=command_timeout)

    # Interfaces in routing-instance internet (show configuration routing-instances internet | display set | match interface)
    internet_interfaces = set()
    send("show configuration routing-instances {} | display set | match interface\r\n".format(UPLINK_VRF_NAME))
    time.sleep(0.2)
    set_output = read_until_prompt(channel, timeout=command_timeout)
    for line in (set_output or "").splitlines():
        line = line.strip()
        if line.startswith("set routing-instances {} interface ".format(UPLINK_VRF_NAME)):
            parts = line.split()
            if len(parts) >= 5:
                internet_interfaces.add(parts[-1])

    _log("SSH: found uplink interfaces (link up): {}".format(len(uplinks)))
    result = []
    aggregates_added = set()

    for logical_name, desc in uplinks:
        is_physical = "." not in logical_name and not logical_name.startswith("ae")
        if is_physical:
            physical_names = [logical_name]
            aggregate_name = None
        else:
            aggregate_name = logical_name.split(".")[0] if "." in logical_name else None
            physical_names = []
            if aggregate_name:
                send("show lacp interfaces {} | display json | no-more\r\n".format(aggregate_name))
                time.sleep(0.2)
                lacp_data = read_until_json_and_prompt(channel, timeout=command_timeout)
                physical_names = _juniper_lacp_member_names(lacp_data) if lacp_data else []
            time.sleep(0.15)

        # Once per aggregate: collect show interfaces aeN data and add a line with isLag for NetBox (LAG / Parent)
        if aggregate_name and aggregate_name not in aggregates_added:
            send("show interfaces {} | display json | no-more\r\n".format(aggregate_name))
            time.sleep(0.2)
            ae_data = read_until_json_and_prompt(channel, timeout=command_timeout)
            ae_stats = {}
            if ae_data:
                ainfos = ae_data.get("interface-information") or []
                if isinstance(ainfos, dict):
                    ainfos = [ainfos]
                for ainfo in ainfos:
                    if not isinstance(ainfo, dict):
                        continue
                    phys_list = ainfo.get("physical-interface") or []
                    if isinstance(phys_list, dict):
                        phys_list = [phys_list]
                    for aph in phys_list:
                        if _juniper_data(aph.get("name")) == aggregate_name:
                            ae_stats = _parse_juniper_phy_iface(aph)
                            break
            agg_row = {
                "name": aggregate_name,
                "description": (ae_stats.get("description") or "").strip() if ae_stats else "",
                "mediaType": None,
                "bandwidth": ae_stats.get("bandwidth") if ae_stats else None,
                "duplex": ae_stats.get("duplex") if ae_stats else None,
                "physicalAddress": ae_stats.get("physicalAddress") if ae_stats else None,
                "mtu": ae_stats.get("mtu") if ae_stats else None,
                "txPower": None,
                "forwardingModel": ae_stats.get("forwardingModel") if ae_stats else None,
            }
            agg_row["physicalInterface"] = aggregate_name
            agg_row["aggregateInterface"] = aggregate_name
            agg_row["logicalInterface"] = logical_name
            agg_row["isLag"] = True
            result.append(agg_row)
            aggregates_added.add(aggregate_name)
            # Line by logical unit 0 (ae5.0): addresses from address-family inet/inet6 → ifa-local
            log_iface_for_unit0 = None
            if ae_data:
                ainfos_ = ae_data.get("interface-information") or []
                if isinstance(ainfos_, dict):
                    ainfos_ = [ainfos_]
                for ainfo_ in ainfos_:
                    if not isinstance(ainfo_, dict):
                        continue
                    phys_list_ = ainfo_.get("physical-interface") or []
                    if isinstance(phys_list_, dict):
                        phys_list_ = [phys_list_] if phys_list_ else []
                    for aph_ in phys_list_:
                        if not isinstance(aph_, dict) or _juniper_data(aph_.get("name")) != aggregate_name:
                            continue
                        logics_ = aph_.get("logical-interface") or []
                        if not isinstance(logics_, list):
                            logics_ = [logics_] if logics_ else []
                        for log_iface in logics_:
                            if isinstance(log_iface, dict) and _juniper_data(log_iface.get("name")) == logical_name:
                                log_iface_for_unit0 = log_iface
                                break
                        if log_iface_for_unit0:
                            break
                    if log_iface_for_unit0:
                        break
            if log_iface_for_unit0:
                addrs = _parse_juniper_logical_ip_addresses(log_iface_for_unit0)
                log_desc = (_juniper_data(log_iface_for_unit0.get("description")) or desc or "").strip()
                first_physical = physical_names[0] if physical_names else None
                logical_row = {
                    "name": logical_name,
                    "description": log_desc,
                    "mediaType": None,
                    "bandwidth": None,
                    "duplex": None,
                    "physicalAddress": None,
                    "mtu": None,
                    "txPower": None,
                    "forwardingModel": None,
                    "physicalInterface": first_physical,
                    "aggregateInterface": aggregate_name,
                    "logicalInterface": logical_name,
                    "isLogical": True,
                    "ipv4_addresses": addrs["ipv4_addresses"],
                    "ipv6_addresses": addrs["ipv6_addresses"],
                }
                # VRF for IP: interface in routing-instance internet (according to config)
                if (addrs["ipv4_addresses"] or addrs["ipv6_addresses"]) and logical_name in internet_interfaces:
                    logical_row["ip_vrf"] = UPLINK_VRF_NAME
                result.append(logical_row)
            time.sleep(0.2)

        for physical_name in physical_names:
            send("show interfaces {} | display json | no-more\r\n".format(physical_name))
            time.sleep(0.2)
            ph_data = read_until_json_and_prompt(channel, timeout=command_timeout)
            physical_stats = {}
            if ph_data:
                pinfos = ph_data.get("interface-information") or []
                if isinstance(pinfos, dict):
                    pinfos = [pinfos]
                for pinfo in pinfos:
                    if not isinstance(pinfo, dict):
                        continue
                    phys = pinfo.get("physical-interface") or []
                    if not isinstance(phys, list):
                        phys = [phys] if phys else []
                    for ph in phys:
                        if _juniper_data(ph.get("name")) == physical_name:
                            physical_stats = _parse_juniper_phy_iface(ph)
                            break
            time.sleep(0.2)

            tx_power = physical_stats.get("txPower")
            if tx_power is None:
                send("show interfaces diagnostics optics {} | display json | no-more\r\n".format(physical_name))
                time.sleep(0.2)
                optics_data = read_until_json_and_prompt(channel, timeout=command_timeout)
                if optics_data:
                    tx_power = _juniper_optics_tx_power_dbm(optics_data)
                time.sleep(0.15)

            media_type = physical_stats.get("mediaType")
            if media_type is None and chassis_hw:
                slot = _juniper_interface_slot(physical_name)
                if slot:
                    media_type = _juniper_chassis_media_type(chassis_hw, slot[0], slot[1], slot[2])
            row = {
                "name": physical_name,
                "description": (physical_stats.get("description") or desc).strip() if physical_stats else desc,
                "mediaType": media_type,
                "bandwidth": physical_stats.get("bandwidth"),
                "duplex": physical_stats.get("duplex"),
                "physicalAddress": physical_stats.get("physicalAddress"),
                "mtu": physical_stats.get("mtu"),
                "txPower": tx_power,
                "forwardingModel": physical_stats.get("forwardingModel"),
            }
            row["physicalInterface"] = physical_name
            row["aggregateInterface"] = aggregate_name
            row["logicalInterface"] = logical_name
            result.append(row)
        time.sleep(0.3)

    client.close()
    _log("SSH: records collected: {}".format(len(result)))
    return result, None


def process_one_arista(device, nb, ssh_user, ssh_pass, ssh_suffix, progress_print, ssh_timeout=45, ssh_command_timeout=90, ssh_config=None):
    """Process one Arista device: SSH + stats collection via uplinks."""
    progress_print(device.name, "connection and collection of uplink stats (Arista)...")
    ssh_host = device.name + ssh_suffix
    connect_host, connect_user = _resolve_ssh_host(ssh_config, device.name, ssh_host, ssh_user)
    log_cb = lambda msg: progress_print(device.name, msg)
    stats, err = get_arista_uplink_stats(connect_host, connect_user, ssh_pass, timeout=ssh_timeout, command_timeout=ssh_command_timeout, log=log_cb)
    if err:
        progress_print(device.name, "error: {}".format(err))
        return device.name, {"error": err}
    progress_print(device.name, "ready ({} interfaces).".format(len(stats)))
    return device.name, stats


def process_one_juniper(device, nb, ssh_user, ssh_pass, ssh_suffix, progress_print, ssh_timeout=45, ssh_command_timeout=90, ssh_config=None):
    """Process one Juniper device: SSH + stats collection via uplinks."""
    progress_print(device.name, "connection and collection of uplink stats (Juniper)...")
    ssh_host = device.name + ssh_suffix
    connect_host, connect_user = _resolve_ssh_host(ssh_config, device.name, ssh_host, ssh_user)
    log_cb = lambda msg: progress_print(device.name, msg)
    stats, err = get_juniper_uplink_stats(connect_host, connect_user, ssh_pass, timeout=ssh_timeout, command_timeout=ssh_command_timeout, log=log_cb)
    if err:
        progress_print(device.name, "error: {}".format(err))
        return device.name, {"error": err}
    progress_print(device.name, "ready ({} interfaces).".format(len(stats)))
    return device.name, stats


def process_one_device_stats(device, nb, ssh_user, ssh_pass, ssh_suffix, progress_print, ssh_timeout=45, ssh_command_timeout=90, ssh_config=None):
    """Process one device: call Arista or Juniper collection by platform; otherwise pass."""
    platform_name = get_device_platform_name(device, nb)
    if is_arista_platform(platform_name):
        return process_one_arista(device, nb, ssh_user, ssh_pass, ssh_suffix, progress_print, ssh_timeout, ssh_command_timeout, ssh_config)
    if is_juniper_platform(platform_name):
        return process_one_juniper(device, nb, ssh_user, ssh_pass, ssh_suffix, progress_print, ssh_timeout, ssh_command_timeout, ssh_config)
    progress_print(device.name, "pass (non-Arista/Juniper): {}".format(platform_name or "no platform"))
    return device.name, None


def _str(v):
    """String representation for the table."""
    if v is None:
        return ""
    return str(v).strip()


def print_table(results):
    """Output results (dict device_name -> list of dicts | {"error": ...}) in a table."""
    headers = ("DEVICE", "INTERFACE", "mediaType", "bandwidth", "duplex", "mtu", "forwardingModel", "txPower", "description")
    rows = []
    desc_col_idx = 8
    for dev_name in sorted(results.keys()):
        payload = results[dev_name]
        if isinstance(payload, dict) and "error" in payload:
            rows.append((dev_name, _str(payload.get("error")), "", "", "", "", "", "", ""))
            continue
        if not isinstance(payload, list):
            continue
        for u in payload:
            rows.append((
                dev_name,
                _str(u.get("name")),
                _str(u.get("mediaType")),
                _str(u.get("bandwidth")),
                _str(u.get("duplex")),
                _str(u.get("mtu")),
                _str(u.get("forwardingModel")),
                _str(u.get("txPower")),
                _str(u.get("description"))[:40],
            ))

    if not rows:
        print("No data to output.")
        return

    col_count = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for c in range(col_count):
            w = len(row[c]) if c < len(row) else 0
            if w > widths[c]:
                widths[c] = min(w, 60 if c == desc_col_idx else 999)

    pad = "  "
    header_line = pad.join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep_line = pad.join("-" * widths[i] for i in range(col_count))
    print(header_line)
    print(sep_line)
    for row in rows:
        parts = []
        for i in range(col_count):
            cell = row[i] if i < len(row) else ""
            parts.append(cell.ljust(widths[i]))
        print(pad.join(parts))
    print("")


def _run_report(netbox_tag, ssh_suffix):
    """Report mode: NetBox vs SSH table by devices with tag."""
    nb = pynetbox.api(os.environ.get("NETBOX_URL"), token=os.environ.get("NETBOX_TOKEN"))
    print("Loading list of devices (tag={})...".format(netbox_tag), flush=True)
    try:
        devices = list(nb.dcim.devices.filter(tag=netbox_tag))
    except Exception as e:
        print("Error accessing NetBox: {}.".format(netbox_error_message(e)), file=sys.stderr)
        return 1
    if not devices:
        print("No devices found with tag '{}'".format(netbox_tag))
        return 0

    max_workers = min(len(devices), max(1, int(os.environ.get("PARALLEL_DEVICES", "6"))))
    print("Found devices: {}. Parallel processing (threads: {}).".format(len(devices), max_workers), flush=True)

    netbox_not_found = "interface not found in NetBox"
    ssh_not_found = "SSH interface not found"
    print_lock = threading.Lock()

    def progress_print(device_name, msg):
        with print_lock:
            print("[{}] {}".format(device_name, msg), flush=True)

    results_by_name = {}
    headers = ("DEVICE", "IP-ADDRESS", "Netbox", "SSH")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_device = {
            executor.submit(
                process_one_device,
                device,
                nb,
                (os.environ.get("SSH_USERNAME") or "").strip(),
                os.environ.get("SSH_PASSWORD"),
                ssh_suffix,
                netbox_not_found,
                ssh_not_found,
                progress_print,
            ): device
            for device in devices
        }
        for future in as_completed(future_to_device):
            device = future_to_device[future]
            try:
                row = future.result()
                results_by_name[device.name] = row
                progress_print(device.name, "ready.")
            except Exception as e:
                progress_print(device.name, "error: {}.".format(e))
                results_by_name[device.name] = (
                    device.name,
                    "",
                    netbox_not_found,
                    "{} (exception: {})".format(ssh_not_found, e),
                )

    rows = [results_by_name[d.name] for d in devices]

    print("", flush=True)
    print("Final table:", flush=True)

    def cell_width(cell):
        return max(len(line) for line in str(cell).splitlines()) if cell else 0

    num_cols = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for c in range(num_cols):
            w = cell_width(row[c])
            if w > widths[c]:
                widths[c] = w

    def fmt_row(cells, padding=2):
        pad = " " * padding
        parts = []
        for c in range(num_cols):
            parts.append(str(cells[c]).split("\n")[0].ljust(widths[c]) if cells[c] else "".ljust(widths[c]))
        return pad.join(parts)

    def fmt_row_all_lines(cells):
        lines_per_cell = [str(cells[c]).split("\n") for c in range(num_cols)]
        max_lines = max(len(lines) for lines in lines_per_cell)
        pad = " " * 2
        out_rows = []
        for i in range(max_lines):
            parts = []
            for c in range(num_cols):
                lines = lines_per_cell[c]
                line = lines[i] if i < len(lines) else ""
                parts.append(line.ljust(widths[c]))
            out_rows.append(pad.join(parts))
        return out_rows

    print(fmt_row(headers))
    print(fmt_row(("",) * num_cols).replace(" ", "-"))
    for row in rows:
        block = fmt_row_all_lines(row)
        for line in block:
            print(line)

    return 0


DEFAULT_STATS_FILE = "dry-ssh.json"


def _load_stats_file(path):
    """Load JSON with the key devices. Return (data, None) or (None, error_msg)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            out = json.load(f)
    except FileNotFoundError:
        return None, "File not found: {}".format(path)
    except json.JSONDecodeError as e:
        return None, "Error parsing JSON in file: {}".format(e)
    if "devices" not in out:
        return None, "The file is expected to contain a structure with the key 'devices'."
    return out, None


def netbox_error_message(e):
    """Convert the exception when accessing NetBox to a short message (for stderr)."""
    err_msg = str(e).strip() if e else "unknown error"
    err_lower = err_msg.lower()
    if "401" in err_msg or "unauthorized" in err_lower or "authentication" in err_lower or "token" in err_lower:
        return "Invalid or expired token. Check NETBOX_TOKEN."
    if "connecttimeout" in err_lower or "timed out" in err_lower or "timeout" in err_lower:
        return "NetBox connection timed out. Check NETBOX_URL and server availability."
    if "connection" in err_lower or "econnrefused" in err_lower or "connect" in err_lower:
        return "Failed to connect to NetBox. Check NETBOX_URL and server availability."
    return err_msg


def main():
    parser = argparse.ArgumentParser(description="Collection and reporting on uplink interfaces (Arista, Juniper)")
    parser.add_argument("--report", action="store_true", help="Report mode: NetBox vs SSH table for all devices with a tag")
    parser.add_argument("--fetch", action="store_true", help="Statistics mode: poll via SSH (otherwise the file is read)")
    parser.add_argument("--platform", choices=("arista", "juniper", "all"), default="all", help="With --fetch: only Arista, only Juniper or all (default: all)")
    parser.add_argument("--host", metavar="NAME", help="When --fetch: poll only the specified host (device name in NetBox)")
    parser.add_argument("--json", action="store_true", help="Output in JSON format (statistics mode)")
    parser.add_argument("--from-file", metavar="FILE", dest="from_file", help="Path to JSON with devices (default {})".format(DEFAULT_STATS_FILE))
    parser.add_argument(
        "--merge-into",
        metavar="FILE",
        nargs="?",
        const=DEFAULT_STATS_FILE,
        default=None,
        help="When --fetch: load FILE, substitute the data for the polled hosts and save back (%s by default). The remaining hosts in the file are not affected." %DEFAULT_STATS_FILE,
    )
    args = parser.parse_args()

    # Read from file mode (default) unless --report or --fetch is requested
    if not args.fetch and not args.report:
        input_file = args.from_file if args.from_file is not None else DEFAULT_STATS_FILE
        out, err = _load_stats_file(input_file)
        if err:
            print(err, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
        else:
            print_table(out["devices"])
        return 0

    if args.report:
        url = os.environ.get("NETBOX_URL")
        token = os.environ.get("NETBOX_TOKEN")
        if not url or not token:
            print("Set the NETBOX_URL and NETBOX_TOKEN variables")
            return 1
        ssh_user = (os.environ.get("SSH_USERNAME") or "").strip()
        ssh_pass = os.environ.get("SSH_PASSWORD")
        if not ssh_user:
            print("Set the SSH_USERNAME variable for SSH access")
            return 1
        if not ssh_pass:
            print("Set the SSH_PASSWORD variable for SSH access")
            return 1
        netbox_tag = os.environ.get("NETBOX_TAG") or "border"
        ssh_suffix = os.environ.get("SSH_HOST_SUFFIX") or ".3hc.io"
        return _run_report(netbox_tag, ssh_suffix)

    # Statistics mode: collection on all supported platforms (Arista + Juniper)
    url = os.environ.get("NETBOX_URL")
    token = os.environ.get("NETBOX_TOKEN")
    if not url or not token:
        print("Set the NETBOX_URL and NETBOX_TOKEN variables")
        return 1

    ssh_user = (os.environ.get("SSH_USERNAME") or "").strip()
    ssh_pass = os.environ.get("SSH_PASSWORD")
    ssh_suffix = os.environ.get("SSH_HOST_SUFFIX") or ".3hc.io"
    try:
        ssh_timeout = max(10, int(os.environ.get("SSH_TIMEOUT", "45")))
    except ValueError:
        ssh_timeout = 45
    try:
        ssh_command_timeout = max(30, int(os.environ.get("SSH_COMMAND_TIMEOUT", "90")))
    except ValueError:
        ssh_command_timeout = 90
    netbox_tag = os.environ.get("NETBOX_TAG") or "border"
    if not ssh_user:
        print("Set the SSH_USERNAME variable for SSH access")
        return 1
    if not ssh_pass:
        print("Set the SSH_PASSWORD variable for SSH access")
        return 1

    nb = pynetbox.api(url, token=token)
    progress_file = sys.stderr if args.json else sys.stdout
    print("Loading devices (tag={})...".format(netbox_tag), flush=True, file=progress_file)
    try:
        devices = list(nb.dcim.devices.filter(tag=netbox_tag))
    except Exception as e:
        print("Error accessing NetBox: {}.".format(netbox_error_message(e)), file=sys.stderr)
        return 1
    if not devices:
        print("No devices found with tag '{}'". format(netbox_tag), file=progress_file)
        return 0

    if args.host:
        devices = [d for d in devices if d.name == args.host]
        if not devices:
            print("Host '{}' was not found in NetBox for tag {}.".format(args.host, netbox_tag), file=sys.stderr)
            return 1

    devices_to_fetch = []
    for d in devices:
        platform_name = get_device_platform_name(d, nb)
        if args.platform == "arista" and is_arista_platform(platform_name):
            devices_to_fetch.append(d)
        elif args.platform == "juniper" and is_juniper_platform(platform_name):
            devices_to_fetch.append(d)
        elif args.platform == "all" and (is_arista_platform(platform_name) or is_juniper_platform(platform_name)):
            devices_to_fetch.append(d)
    if not devices_to_fetch:
        print("No devices found by filter (platform={}, host={})".format(args.platform, args.host or "all"), file=sys.stderr)
        return 0

    n_arista = sum(1 for d in devices_to_fetch if is_arista_platform(get_device_platform_name(d, nb)))
    n_juniper = len(devices_to_fetch) - n_arista
    max_workers = min(len(devices_to_fetch), max(1, int(os.environ.get("PARALLEL_DEVICES", "6"))))
    host_note = "host {}".format(args.host) if args.host else ""
    print("Devices{}: {} (Arista: {}, Juniper: {}). Threads: {}.".format(host_note, len(devices_to_fetch), n_arista, n_juniper, max_workers), flush=True, file=progress_file)

    # By default we use ~/.ssh/config (HostName, User); disable: USE_SSH_CONFIG=0
    use_ssh_config = os.environ.get("USE_SSH_CONFIG", "1").strip().lower() not in ("0", "false", "no")
    ssh_config = _load_ssh_config() if use_ssh_config else None

    print_lock = threading.Lock()
    def progress_print(device_name, msg):
        with print_lock:
            print("[{}] {}".format(device_name, msg), flush=True, file=progress_file)

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_device = {
            executor.submit(
                process_one_device_stats,
                device,
                nb,
                ssh_user,
                ssh_pass,
                ssh_suffix,
                progress_print,
                ssh_timeout,
                ssh_command_timeout,
                ssh_config,
            ): device
            for device in devices_to_fetch
        }
        for future in as_completed(future_to_device):
            device = future_to_device[future]
            try:
                name, data = future.result()
                results[name] = data
            except Exception as e:
                progress_print(device.name, "exception: {}.".format(e))
                results[device.name] = {"error": str(e)}

    out = {"devices": {dev_name: payload for dev_name, payload in results.items()}}
    if getattr(args, "merge_into", None) is not None:
        merge_path = args.merge_into
        merged, load_err = _load_stats_file(merge_path)
        if merged is None and load_err and "not found" not in load_err:
            print("--merge-into: {}.".format(load_err), file=sys.stderr)
            return 1
        if merged is None:
            merged = {"devices": {}}
        for dev_name, payload in results.items():
            merged["devices"][dev_name] = payload
        try:
            with open(merge_path, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
        except OSError as e:
            print("--merge-into: failed to write {}: {}.".format(merge_path, e), file=sys.stderr)
            return 1
        out = merged
        print("Updated file {} (hosts in file: {}).".format(merge_path, len(merged["devices"])), flush=True, file=progress_file)
    print("", flush=True, file=progress_file)

    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print_table(out["devices"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
