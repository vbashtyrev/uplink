#!/usr/bin/env python3
"""
Сбор и отчёт по uplink-интерфейсам (Arista, Juniper). Два режима:

1) Режим отчёта (--report): устройства по тегу из NetBox, таблица NetBox vs SSH
   (interface + description). Поддержка Juniper и Arista по platform.name.

2) Режим статистики: по умолчанию чтение из файла; с --fetch — опрос по SSH устройств
   Arista и Juniper (по тегу из NetBox). Для каждого uplink'а собираются поля в едином
   формате (name, description, bandwidth, mtu и т.д.). Вывод — таблица или JSON.

Переменные: NETBOX_URL, NETBOX_TOKEN, SSH_USERNAME, SSH_PASSWORD, SSH_HOST_SUFFIX,
PARALLEL_DEVICES, NETBOX_TAG. Опционально: DEBUG_SSH_JSON=1 (режим отчёта).
"""

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


def _format_ssh_connect_error(host, e):
    """
    Детальное описание ошибки при SSH-подключении к host: тип исключения, сообщение, errno.
    Помогает различать timeout, Network unreachable, refused и т.д.
    """
    exc_type = type(e).__name__
    msg = (str(e).strip() if e else "") or "(нет сообщения)"
    line = "SSH: ошибка при подключении к {!r}: {} — {}".format(host, exc_type, msg)
    if isinstance(e, OSError) and getattr(e, "errno", None) is not None:
        line += " (errno {})".format(e.errno)
    return line


def _load_ssh_config():
    """Загрузить ~/.ssh/config для подстановки HostName/User. Возврат SSHConfig или None."""
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
    """По ~/.ssh/config подставить HostName и User. Возврат (host, user)."""
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
    """Извлечь первый валидный JSON из вывода с приглашением и мусором."""
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
    """Из элемента physical-interface или logical-interface вытащить (name, description)."""
    name_list = iface_dict.get("name") or [{}]
    desc_list = iface_dict.get("description") or [{}]
    name = (name_list[0].get("data") or "").strip()
    desc = (desc_list[0].get("data") or "").strip()
    return name, desc


def _juniper_iface_oper_status(iface_dict):
    """Из элемента physical-interface или logical-interface вытащить oper-status (up/down)."""
    return _juniper_data(iface_dict.get("oper-status"))


def _juniper_data(field):
    """Из поля Junos (список с dict с ключом data) вытащить строку или число."""
    if not field:
        return None
    if isinstance(field, list) and field and isinstance(field[0], dict):
        val = field[0].get("data")
        if val is not None:
            s = str(val).strip()
            return s if s else None
    return None


def _juniper_speed_to_bps(speed_str):
    """Junos speed (например 1000mbps, 10gbps) в bps. Возврат int или None."""
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
    """True, если интерфейс считаем upstream: физический (без точки) или unit 0 (*.0). Unit не 0 — VLAN, не проверяем."""
    if not name:
        return False
    if "." not in name:
        return True
    return name.split(".")[-1] == "0"


def parse_juniper_uplinks(json_data, require_link_up=False):
    """
    Из Juniper JSON вытащить интерфейсы с 'Uplink:' в description (physical + logical).
    Если require_link_up=True — только с oper-status == 'up'.
    Учитываются только unit 0 (*.0) или физические; unit не 0 (VLAN) пропускаются.
    Возврат: [(name, desc), ...]
    """
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
    """Из вывода Junos (display xml) вырезать один блок <interface-information>...</interface-information>."""
    blocks = _extract_all_xml_interface_information_blocks(text)
    return blocks[0] if blocks else None


def _extract_all_xml_interface_information_blocks(text):
    """Из вывода Junos (display xml) вырезать все блоки <interface-information>...</interface-information>."""
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
    """
    Распарсить полный документ <rpc-reply> (чтобы были все xmlns), вернуть список
    корневых элементов interface-information (каждый — уже распарсенное дерево).
    Возврат: [elem, ...] или [] при ошибке.
    """
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
    """Из элемента XML Junos (с вложенным <data> или текстом) вытащить строку."""
    if elem is None:
        return ""
    for child in elem:
        if child.tag.split("}")[-1] == "data" and child.text:
            return (child.text or "").strip()
    return (elem.text or "").strip()


def _juniper_xml_child(elem, local_name):
    """Найти дочерний элемент по локальному имени тега (без namespace)."""
    if elem is None:
        return None
    for c in elem:
        if c.tag.split("}")[-1] == local_name:
            return c
    return None


def _juniper_xml_iface_name_desc_oper(elem):
    """Из элемента physical-interface или logical-interface (XML) вытащить (name, description, oper_status)."""
    name_el = _juniper_xml_child(elem, "name")
    desc_el = _juniper_xml_child(elem, "description")
    oper_el = _juniper_xml_child(elem, "oper-status")
    name = _juniper_xml_elem_text(name_el) if name_el is not None else ""
    desc = _juniper_xml_elem_text(desc_el) if desc_el is not None else ""
    oper = _juniper_xml_elem_text(oper_el) if oper_el is not None else None
    return name, desc, oper


def parse_juniper_uplinks_from_xml(xml_root, require_link_up=False, debug_cb=None):
    """
    Из корня XML (interface-information) вытащить все интерфейсы с 'Uplink:' в description.
    XML не теряет дубликаты тегов (в отличие от JSON). Обходим все узлы (в т.ч. вложенные logical-interface).
    Возврат: [(name, desc), ...]
    debug_cb(msg) — при отладке вызывается для каждого рассматриваемого интерфейса.
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
                debug_cb("    -> пропуск: нет Uplink в desc")
            continue
        if require_link_up and oper is not None and oper.lower() != "up":
            if debug_cb:
                debug_cb("    -> пропуск: oper не up")
            continue
        if not _juniper_uplink_is_unit0(name):
            if debug_cb:
                debug_cb("    -> пропуск: не unit 0")
            continue
        if debug_cb:
            debug_cb("    -> добавлен")
        out.append((name, desc))
    return out


def parse_juniper_descriptions_all(json_data):
    """
    Из Juniper JSON (show interfaces descriptions) вытащить все интерфейсы.
    Возврат: [(name, description, oper_status), ...]; oper_status может быть None.
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
    """Из Arista JSON вытащить интерфейсы с 'Uplink:' в description."""
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
    """True, если по данным show interfaces интерфейс считается поднятым (link up)."""
    if not if_obj:
        return False
    line_proto = (if_obj.get("lineProtocolStatus") or "").strip().lower()
    iface_status = (if_obj.get("interfaceStatus") or "").strip().lower()
    if line_proto == "down":
        return False
    if iface_status in ("disabled", "notconnect", "down"):
        return False
    return True


def arista_cli_interface_name(name):
    """Ethernet72/1 -> ethernet 72/1 для команды show int ..."""
    return re.sub(r"([a-zA-Z]+)(\d)", r"\1 \2", name).strip().lower()


def is_juniper_platform(platform_name):
    """По имени платформы из NetBox: JunOS / Juniper → True."""
    if not platform_name:
        return False
    n = platform_name.lower()
    return "junos" in n or "juniper" in n


def is_arista_platform(platform_name):
    """По имени платформы из NetBox: Arista EOS → True."""
    if not platform_name:
        return False
    n = platform_name.lower()
    return "arista" in n or "eos" in n


def get_device_platform_name(device, nb):
    """Имя платформы из NetBox (device.platform.name) для определения Juniper/Arista."""
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
    Подключиться по SSH, выполнить команду и вернуть список (interface, description) с 'Uplink:'.
    Тип устройства: platform_name (NetBox) или по баннеру (JUNOS). log — callback, debug_json — вывод JSON.
    """
    def _log(msg):
        if log:
            log(msg)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        _log("SSH: подключение к {}...".format(host))
        client.connect(
            host,
            username=username,
            password=password,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        _log("SSH: подключено")
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
            _log("SSH: платформа '{}' — считаем Arista".format(platform_name))
    else:
        is_juniper = "JUNOS" in out_after_pass
    _log("SSH: определено как {}".format("Juniper" if is_juniper else "Arista"))

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
                    _log("--- SSH JSON не извлечён для {} (до 3000 символов) ---\n{}".format(iface_name, (output[:3000] if output else "(пусто)")))
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
                _log("--- SSH JSON не извлечён. Сырой вывод (до 6000 символов) ---\n" + (output[:6000] if output else "(пусто)"))
        if not data:
            client.close()
            _log("SSH: не удалось извлечь JSON из вывода")
            return None, "не удалось извлечь JSON из вывода"
        if is_juniper:
            uplinks = parse_juniper_uplinks(data)
        else:
            uplinks = parse_arista_uplinks(data)

    client.close()
    _log("SSH: готово ({} uplinks)".format(len(uplinks)))
    return sorted(uplinks, key=lambda x: x[0]), None


def format_cell(lines, not_found_comment):
    """Оформить список (name, desc) в текст ячейки или комментарий."""
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
    """Обработать одно устройство: NetBox + SSH. Возвращает (name, ip, netbox_cell, ssh_cell)."""
    progress_print(device.name, "NetBox: получение интерфейсов...")
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


# --- Режим статистики Arista: read_until по channel, возврат данных ---
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
    """Читать вывод до приглашения и извлечь JSON."""
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
    """Проверить, что в конце буфера есть приглашение CLI (user@host> или host#), а не просто '>' из XML/JSON."""
    if not text or not text.strip():
        return False
    last_line = text.split("\n")[-1].strip() if "\n" in text else text.strip()
    if not last_line:
        return False
    if not (last_line.endswith(">") or last_line.endswith("#")):
        return False
    return "@" in last_line


def read_until_prompt(channel, timeout=120):
    """Читать вывод до приглашения CLI в конце (user@host> или host#), вернуть сырой текст. Не возвращаться на первый '>' в XML/JSON."""
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
    SSH к Arista: список интерфейсов с "Uplink:", для каждого show interfaces + transceiver,
    при bridged — switchport configuration source. Возврат: список dict или (None, error).
    """
    def _log(msg):
        if log:
            log(msg)

    start_time = time.monotonic()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        _log("SSH: подключение к {}...".format(host))
        client.connect(
            host,
            username=username,
            password=password,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        _log("SSH: подключено")
    except (socket.timeout, paramiko.SSHException, OSError) as e:
        elapsed = time.monotonic() - start_time
        _log(_format_ssh_connect_error(host, e))
        _log("SSH: с начала попытки прошло {:.0f} с".format(elapsed))
        return None, "{} (через {:.0f} с)".format(str(e), elapsed)

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
        _log("SSH: не удалось получить show interfaces description | json | no-more")
        _log("SSH: с начала попытки прошло {:.0f} с".format(elapsed))
        return None, "не удалось получить show interfaces description | json (через {:.0f} с)".format(elapsed)

    uplinks = parse_arista_uplinks(desc_data)
    if not uplinks:
        client.close()
        _log("SSH: uplink-интерфейсов не найдено")
        return [], None

    _log("SSH: найдено uplink-интерфейсов: {} (в отчёт только с link up)".format(len(uplinks)))
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
        result.append(row)
        time.sleep(0.3)

    client.close()
    _log("SSH: собрано записей: {}".format(len(result)))
    return result, None


def _parse_juniper_logical_mtu(log_iface):
    """Из logical-interface Junos взять MTU из первого address-family с числовым mtu."""
    afs = log_iface.get("address-family") or []
    if not isinstance(afs, list):
        afs = [afs] if afs else []
    for af in afs:
        mtu_raw = _juniper_data(af.get("mtu"))
        if mtu_raw and str(mtu_raw).isdigit():
            return int(mtu_raw)
    return None


def _juniper_ae_bundle_name(iface_json):
    """
    Из JSON вывода show interfaces <name> | display json вытащить ae-bundle-name
    (интерфейс в LAG: logical-interface → address-family aenet → ae-bundle-name).
    Возврат: строка типа "ae5.0" или None.
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
    Из JSON вывода show lacp interfaces <ae> | display json вытащить имена
    физических членов LAG (lag-lacp-state / lag-lacp-protocol → name).
    Возврат: список строк ["et-0/0/3", ...] без дубликатов.
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
    Из имени интерфейса Junos (et-0/0/3, xe-0/1/0) вытащить (fpc, pic, port).
    Соответствие: type-fpc/pic/port → FPC fpc, PIC pic, Xcvr port. Возврат (int,int,int) или None.
    """
    if not iface_name:
        return None
    m = re.match(r"^[a-zA-Z]+-(\d+)/(\d+)/(\d+)$", iface_name.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _juniper_optics_tx_power_dbm(diag_json):
    """
    Из JSON show interfaces diagnostics optics <name> | display json вытащить
    среднее laser-output-power-dbm по всем lane (optics-diagnostics-lane-values).
    Возврат: float (dBm) или None.
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
    Из JSON show chassis hardware | display json вытащить модель SFP (description)
    для слота FPC fpc, PIC pic, Xcvr port. Возврат строка или None.
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
    """Из physical-interface Junos извлечь поля в виде dict (те же ключи, что у Arista)."""
    name = _juniper_data(ph.get("name"))
    desc = _juniper_data(ph.get("description")) or ""
    speed_str = _juniper_data(ph.get("speed"))
    bandwidth = _juniper_speed_to_bps(speed_str)
    mtu_raw = _juniper_data(ph.get("mtu"))
    mtu = int(mtu_raw) if mtu_raw and str(mtu_raw).isdigit() else None
    mac = _juniper_data(ph.get("current-physical-address"))
    # link-type в Junos: Full-Duplex, Half-Duplex (на 10G/40G/100G Junos часто не выводит — по стандарту только full)
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
        duplex = "full"  # 10G+ только full duplex, half не определён в стандартах
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
    SSH к Juniper (Junos): список интерфейсов с "Uplink:" в description,
    для каждого show interfaces <name> detail | display json. Возврат: список dict (тот же формат, что Arista) или (None, error).
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
        _log("SSH: подключение к {}...".format(host))
        client.connect(
            host,
            username=username,
            password=password,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        _log("SSH: подключено")
    except (socket.timeout, paramiko.SSHException, OSError) as e:
        elapsed = time.monotonic() - start_time
        _log(_format_ssh_connect_error(host, e))
        _log("SSH: с начала попытки прошло {:.0f} с".format(elapsed))
        return None, "{} (через {:.0f} с)".format(str(e), elapsed)

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
        _log("SSH: не удалось получить show interfaces descriptions | display json")
        _log("SSH: с начала попытки прошло {:.0f} с".format(elapsed))
        return None, "не удалось получить show interfaces descriptions | display json (через {:.0f} с)".format(elapsed)

    uplinks = parse_juniper_uplinks(desc_data, require_link_up=True)
    _dbg("Шаг 1 (JSON): parse_juniper_uplinks(require_link_up=True) вернул {} записей".format(len(uplinks)))
    if not uplinks:
        _log("SSH: в JSON uplink'ов не найдено, пробуем display xml (дубликаты ключей в JSON)...")
        send("show interfaces descriptions | display xml | no-more\r\n")
        time.sleep(0.2)
        xml_text = read_until_prompt(channel, timeout=command_timeout)
        _dbg("Шаг 2 (XML сырой): len(xml_text)={}, _looks_like_cli_prompt={}".format(len(xml_text), _looks_like_cli_prompt(xml_text)))
        _dbg("Шаг 2: начало (300 символов): {!r}".format(xml_text[:300] if len(xml_text) >= 300 else xml_text[:]))
        _dbg("Шаг 2: конец (300 символов): {!r}".format(xml_text[-300:] if len(xml_text) > 300 else ""))
        interface_information_roots = _parse_junos_rpc_reply_and_find_interface_information(xml_text)
        _dbg("Шаг 3 (парсинг rpc-reply): найдено элементов interface-information: {}".format(len(interface_information_roots)))
        for i, root in enumerate(interface_information_roots):
            _dbg("Шаг 3: корень[{}] tag={}".format(i, root.tag))
        for root in interface_information_roots:
            from_block = parse_juniper_uplinks_from_xml(root, require_link_up=True, debug_cb=_dbg if debug else None)
            _dbg("Шаг 4 (парсинг блока): parse_juniper_uplinks_from_xml вернул {} записей: {}".format(len(from_block), from_block))
            uplinks.extend(from_block)
        seen = set()
        uplinks = [(n, d) for n, d in uplinks if n not in seen and not seen.add(n)]
        _dbg("Шаг 5 (после дедупликации): всего uplinks: {}".format(len(uplinks)))
        if not uplinks:
            client.close()
            _log("SSH: uplink-интерфейсов с Link up не найдено")
            return [], None
        time.sleep(0.2)

    send("show chassis hardware | display json | no-more\r\n")
    time.sleep(0.2)
    chassis_hw = read_until_json_and_prompt(channel, timeout=command_timeout)

    _log("SSH: найдено uplink-интерфейсов (link up): {}".format(len(uplinks)))
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

        # Один раз на агрегат: собрать данные show interfaces aeN и добавить строку с isLag для NetBox (LAG / Parent)
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
    _log("SSH: собрано записей: {}".format(len(result)))
    return result, None


def process_one_arista(device, nb, ssh_user, ssh_pass, ssh_suffix, progress_print, ssh_timeout=45, ssh_command_timeout=90, ssh_config=None):
    """Обработать одно устройство Arista: SSH + сбор stats по uplinks."""
    progress_print(device.name, "подключение и сбор uplink stats (Arista)...")
    ssh_host = device.name + ssh_suffix
    connect_host, connect_user = _resolve_ssh_host(ssh_config, device.name, ssh_host, ssh_user)
    log_cb = lambda msg: progress_print(device.name, msg)
    stats, err = get_arista_uplink_stats(connect_host, connect_user, ssh_pass, timeout=ssh_timeout, command_timeout=ssh_command_timeout, log=log_cb)
    if err:
        progress_print(device.name, "ошибка: {}".format(err))
        return device.name, {"error": err}
    progress_print(device.name, "готово ({} интерфейсов).".format(len(stats)))
    return device.name, stats


def process_one_juniper(device, nb, ssh_user, ssh_pass, ssh_suffix, progress_print, ssh_timeout=45, ssh_command_timeout=90, ssh_config=None):
    """Обработать одно устройство Juniper: SSH + сбор stats по uplinks."""
    progress_print(device.name, "подключение и сбор uplink stats (Juniper)...")
    ssh_host = device.name + ssh_suffix
    connect_host, connect_user = _resolve_ssh_host(ssh_config, device.name, ssh_host, ssh_user)
    log_cb = lambda msg: progress_print(device.name, msg)
    stats, err = get_juniper_uplink_stats(connect_host, connect_user, ssh_pass, timeout=ssh_timeout, command_timeout=ssh_command_timeout, log=log_cb)
    if err:
        progress_print(device.name, "ошибка: {}".format(err))
        return device.name, {"error": err}
    progress_print(device.name, "готово ({} интерфейсов).".format(len(stats)))
    return device.name, stats


def process_one_device_stats(device, nb, ssh_user, ssh_pass, ssh_suffix, progress_print, ssh_timeout=45, ssh_command_timeout=90, ssh_config=None):
    """Обработать одно устройство: по платформе вызвать Arista или Juniper сбор; иначе пропуск."""
    platform_name = get_device_platform_name(device, nb)
    if is_arista_platform(platform_name):
        return process_one_arista(device, nb, ssh_user, ssh_pass, ssh_suffix, progress_print, ssh_timeout, ssh_command_timeout, ssh_config)
    if is_juniper_platform(platform_name):
        return process_one_juniper(device, nb, ssh_user, ssh_pass, ssh_suffix, progress_print, ssh_timeout, ssh_command_timeout, ssh_config)
    progress_print(device.name, "пропуск (не Arista/Juniper): {}".format(platform_name or "нет платформы"))
    return device.name, None


def _str(v):
    """Строковое представление для таблицы."""
    if v is None:
        return ""
    return str(v).strip()


def print_table(results):
    """Вывести results (dict device_name -> list of dicts | {"error": ...}) таблицей."""
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
        print("Нет данных для вывода.")
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
    """Режим отчёта: таблица NetBox vs SSH по устройствам с тегом."""
    nb = pynetbox.api(os.environ.get("NETBOX_URL"), token=os.environ.get("NETBOX_TOKEN"))
    print("Загрузка списка устройств (tag={})...".format(netbox_tag), flush=True)
    try:
        devices = list(nb.dcim.devices.filter(tag=netbox_tag))
    except Exception as e:
        print("Ошибка доступа к NetBox: {}.".format(netbox_error_message(e)), file=sys.stderr)
        return 1
    if not devices:
        print("Устройств с тегом '{}' не найдено".format(netbox_tag))
        return 0

    max_workers = min(len(devices), max(1, int(os.environ.get("PARALLEL_DEVICES", "6"))))
    print("Найдено устройств: {}. Параллельная обработка (потоков: {}).".format(len(devices), max_workers), flush=True)

    netbox_not_found = "интерфейс в NetBox не найден"
    ssh_not_found = "интерфейс в SSH не найден"
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
                os.environ.get("SSH_USERNAME", "admin"),
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
                progress_print(device.name, "готово.")
            except Exception as e:
                progress_print(device.name, "ошибка: {}.".format(e))
                results_by_name[device.name] = (
                    device.name,
                    "",
                    netbox_not_found,
                    "{} (исключение: {})".format(ssh_not_found, e),
                )

    rows = [results_by_name[d.name] for d in devices]

    print("", flush=True)
    print("Итоговая таблица:", flush=True)

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
    """Загрузить JSON с ключом devices. Возврат (data, None) или (None, error_msg)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            out = json.load(f)
    except FileNotFoundError:
        return None, "Файл не найден: {}".format(path)
    except json.JSONDecodeError as e:
        return None, "Ошибка разбора JSON в файле: {}".format(e)
    if "devices" not in out:
        return None, "В файле ожидается структура с ключом 'devices'."
    return out, None


def netbox_error_message(e):
    """Преобразовать исключение при обращении к NetBox в короткое сообщение (для stderr)."""
    err_msg = str(e).strip() if e else "неизвестная ошибка"
    err_lower = err_msg.lower()
    if "401" in err_msg or "unauthorized" in err_lower or "authentication" in err_lower or "token" in err_lower:
        return "Неверный или просроченный токен. Проверьте NETBOX_TOKEN."
    if "connecttimeout" in err_lower or "timed out" in err_lower or "timeout" in err_lower:
        return "Таймаут подключения к NetBox. Проверьте NETBOX_URL и доступность сервера."
    if "connection" in err_lower or "econnrefused" in err_lower or "connect" in err_lower:
        return "Не удалось подключиться к NetBox. Проверьте NETBOX_URL и доступность сервера."
    return err_msg


def main():
    parser = argparse.ArgumentParser(description="Сбор и отчёт по uplink-интерфейсам (Arista, Juniper)")
    parser.add_argument("--report", action="store_true", help="Режим отчёта: таблица NetBox vs SSH по всем устройствам с тегом")
    parser.add_argument("--fetch", action="store_true", help="Режим статистики: опросить по SSH (иначе читается файл)")
    parser.add_argument("--platform", choices=("arista", "juniper", "all"), default="all", help="При --fetch: только Arista, только Juniper или все (по умолчанию: all)")
    parser.add_argument("--host", metavar="NAME", help="При --fetch: опросить только указанный хост (имя устройства в NetBox)")
    parser.add_argument("--json", action="store_true", help="Вывод в формате JSON (режим статистики)")
    parser.add_argument("--from-file", metavar="FILE", dest="from_file", help="Путь к JSON с devices (по умолчанию {})".format(DEFAULT_STATS_FILE))
    parser.add_argument(
        "--merge-into",
        metavar="FILE",
        nargs="?",
        const=DEFAULT_STATS_FILE,
        default=None,
        help="При --fetch: загрузить FILE, подставить данные по опрошенным хостам и сохранить обратно (по умолчанию %s). Остальные хосты в файле не трогаются." % DEFAULT_STATS_FILE,
    )
    args = parser.parse_args()

    # Режим «чтение из файла» (по умолчанию), если не запрошены --report или --fetch
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
            print("Задайте переменные NETBOX_URL и NETBOX_TOKEN")
            return 1
        ssh_pass = os.environ.get("SSH_PASSWORD")
        if not ssh_pass:
            print("Задайте переменную SSH_PASSWORD для доступа по SSH")
            return 1
        netbox_tag = os.environ.get("NETBOX_TAG") or "border"
        ssh_suffix = os.environ.get("SSH_HOST_SUFFIX") or ".3hc.io"
        return _run_report(netbox_tag, ssh_suffix)

    # Режим статистики: сбор по всем поддерживаемым платформам (Arista + Juniper)
    url = os.environ.get("NETBOX_URL")
    token = os.environ.get("NETBOX_TOKEN")
    if not url or not token:
        print("Задайте переменные NETBOX_URL и NETBOX_TOKEN")
        return 1

    ssh_user = os.environ.get("SSH_USERNAME", "admin")
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
    if not ssh_pass:
        print("Задайте переменную SSH_PASSWORD для доступа по SSH")
        return 1

    nb = pynetbox.api(url, token=token)
    progress_file = sys.stderr if args.json else sys.stdout
    print("Загрузка устройств (tag={})...".format(netbox_tag), flush=True, file=progress_file)
    try:
        devices = list(nb.dcim.devices.filter(tag=netbox_tag))
    except Exception as e:
        print("Ошибка доступа к NetBox: {}.".format(netbox_error_message(e)), file=sys.stderr)
        return 1
    if not devices:
        print("Устройств с тегом '{}' не найдено".format(netbox_tag), file=progress_file)
        return 0

    if args.host:
        devices = [d for d in devices if d.name == args.host]
        if not devices:
            print("Хост '{}' не найден в NetBox по тегу {}.".format(args.host, netbox_tag), file=sys.stderr)
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
        print("Устройств по фильтру (platform={}, host={}) не найдено.".format(args.platform, args.host or "все"), file=sys.stderr)
        return 0

    n_arista = sum(1 for d in devices_to_fetch if is_arista_platform(get_device_platform_name(d, nb)))
    n_juniper = len(devices_to_fetch) - n_arista
    max_workers = min(len(devices_to_fetch), max(1, int(os.environ.get("PARALLEL_DEVICES", "6"))))
    host_note = " хост {}".format(args.host) if args.host else ""
    print("Устройств{}: {} (Arista: {}, Juniper: {}). Потоков: {}.".format(host_note, len(devices_to_fetch), n_arista, n_juniper, max_workers), flush=True, file=progress_file)

    use_ssh_config = os.environ.get("USE_SSH_CONFIG", "").strip().lower() in ("1", "true", "yes")
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
                progress_print(device.name, "исключение: {}.".format(e))
                results[device.name] = {"error": str(e)}

    out = {"devices": {dev_name: payload for dev_name, payload in results.items()}}
    if getattr(args, "merge_into", None) is not None:
        merge_path = args.merge_into
        merged, load_err = _load_stats_file(merge_path)
        if merged is None and load_err and "не найден" not in load_err:
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
            print("--merge-into: не удалось записать {}: {}.".format(merge_path, e), file=sys.stderr)
            return 1
        out = merged
        print("Обновлён файл {} (хостов в файле: {}).".format(merge_path, len(merged["devices"])), flush=True, file=progress_file)
    print("", flush=True, file=progress_file)

    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print_table(out["devices"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
