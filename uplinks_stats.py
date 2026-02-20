#!/usr/bin/env python3
"""
Сбор и отчёт по uplink-интерфейсам (Arista, Juniper). Два режима:

1) Режим отчёта (--report): устройства по тегу из NetBox, таблица NetBox vs SSH
   (interface + description). Поддержка Juniper и Arista по platform.name.

2) Режим статистики (по умолчанию): сбор по Arista (устройства по тегу), для каждого
   uplink'а — show interfaces, transceiver, опционально switchport; вывод таблица или JSON.
   Ключ --from-file: чтение готового JSON без SSH.

Переменные: NETBOX_URL, NETBOX_TOKEN, SSH_USERNAME, SSH_PASSWORD, SSH_HOST_SUFFIX,
PARALLEL_DEVICES, NETBOX_TAG. Опционально: DEBUG_SSH_JSON=1 (режим отчёта).
"""

import argparse
import json
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import paramiko
import pynetbox


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


def parse_juniper_uplinks(json_data):
    """Из Juniper JSON вытащить интерфейсы с 'Uplink:' в description (physical + logical)."""
    out = []
    infos = json_data.get("interface-information") or []
    if isinstance(infos, dict):
        infos = [infos]
    for info in infos:
        for ph in info.get("physical-interface") or []:
            name, desc = _juniper_iface_name_desc(ph)
            if name and "Uplink:" in desc:
                out.append((name, desc))
        for log in info.get("logical-interface") or []:
            name, desc = _juniper_iface_name_desc(log)
            if name and "Uplink:" in desc:
                out.append((name, desc))
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
        _log("SSH: ошибка — {}".format(e))
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


def get_arista_uplink_stats(host, username, password, timeout=45, command_timeout=90, log=None):
    """
    SSH к Arista: список интерфейсов с "Uplink:", для каждого show interfaces + transceiver,
    при bridged — switchport configuration source. Возврат: список dict или (None, error).
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
        _log("SSH: ошибка — {}".format(e))
        return None, str(e)

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
        client.close()
        _log("SSH: не удалось получить show interfaces description | json | no-more")
        return None, "не удалось получить show interfaces description | json"

    uplinks = parse_arista_uplinks(desc_data)
    if not uplinks:
        client.close()
        _log("SSH: uplink-интерфейсов не найдено")
        return [], None

    _log("SSH: найдено uplink-интерфейсов: {}".format(len(uplinks)))
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


def process_one_arista(device, nb, ssh_user, ssh_pass, ssh_suffix, progress_print):
    """Обработать одно устройство Arista: SSH + сбор stats по uplinks."""
    progress_print(device.name, "подключение и сбор uplink stats...")
    platform_name = get_device_platform_name(device, nb)
    if not is_arista_platform(platform_name):
        progress_print(device.name, "пропуск (не Arista): {}".format(platform_name or "нет платформы"))
        return device.name, None

    ssh_host = device.name + ssh_suffix
    log_cb = lambda msg: progress_print(device.name, msg)
    stats, err = get_arista_uplink_stats(ssh_host, ssh_user, ssh_pass, log=log_cb)
    if err:
        progress_print(device.name, "ошибка: {}".format(err))
        return device.name, {"error": err}
    progress_print(device.name, "готово ({} интерфейсов).".format(len(stats)))
    return device.name, stats


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
    devices = list(nb.dcim.devices.filter(tag=netbox_tag))
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


def main():
    parser = argparse.ArgumentParser(description="Сбор и отчёт по uplink-интерфейсам (Arista, Juniper)")
    parser.add_argument("--report", action="store_true", help="Режим отчёта: таблица NetBox vs SSH по всем устройствам с тегом")
    parser.add_argument("--json", action="store_true", help="Вывод в формате JSON (режим статистики)")
    parser.add_argument("--from-file", metavar="FILE", dest="from_file", help="Не опрашивать SSH; взять данные из JSON-файла и вывести таблицу/JSON")
    args = parser.parse_args()

    if args.from_file:
        try:
            with open(args.from_file, "r", encoding="utf-8") as f:
                out = json.load(f)
        except FileNotFoundError:
            print("Файл не найден: {}".format(args.from_file))
            return 1
        except json.JSONDecodeError as e:
            print("Ошибка разбора JSON в файле: {}".format(e))
            return 1
        if "devices" not in out:
            print("В файле ожидается структура с ключом 'devices'.")
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

    # Режим статистики Arista
    url = os.environ.get("NETBOX_URL")
    token = os.environ.get("NETBOX_TOKEN")
    if not url or not token:
        print("Задайте переменные NETBOX_URL и NETBOX_TOKEN")
        return 1

    ssh_user = os.environ.get("SSH_USERNAME", "admin")
    ssh_pass = os.environ.get("SSH_PASSWORD")
    ssh_suffix = os.environ.get("SSH_HOST_SUFFIX") or ".3hc.io"
    netbox_tag = os.environ.get("NETBOX_TAG") or "border"
    if not ssh_pass:
        print("Задайте переменную SSH_PASSWORD для доступа по SSH")
        return 1

    nb = pynetbox.api(url, token=token)
    print("Загрузка устройств (tag={})...".format(netbox_tag), flush=True)
    devices = list(nb.dcim.devices.filter(tag=netbox_tag))
    if not devices:
        print("Устройств с тегом '{}' не найдено".format(netbox_tag))
        return 0

    arista_devices = []
    for d in devices:
        platform_name = get_device_platform_name(d, nb)
        if is_arista_platform(platform_name):
            arista_devices.append(d)
    if not arista_devices:
        print("Устройств Arista среди выбранных не найдено")
        return 0

    max_workers = min(len(arista_devices), max(1, int(os.environ.get("PARALLEL_DEVICES", "6"))))
    print("Arista устройств: {}. Потоков: {}.".format(len(arista_devices), max_workers), flush=True)

    print_lock = threading.Lock()
    def progress_print(device_name, msg):
        with print_lock:
            print("[{}] {}".format(device_name, msg), flush=True)

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_device = {
            executor.submit(
                process_one_arista,
                device,
                nb,
                ssh_user,
                ssh_pass,
                ssh_suffix,
                progress_print,
            ): device
            for device in arista_devices
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
    print("", flush=True)

    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print_table(out["devices"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
