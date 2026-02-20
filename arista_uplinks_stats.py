#!/usr/bin/env python3
"""
Сбор данных по uplink-интерфейсам Arista из Netbox (устройства по тегу).
Подключение по SSH: device.name + SSH_HOST_SUFFIX.
Только устройства с платформой Arista EOS. Для каждого устройства ищем интерфейсы
с "Uplink:" в description, затем для каждого выполняем:
  show interfaces <name> | json | no-more
  show interfaces <name> transceiver | json | no-more
и извлекаем: name, mediaType, bandwidth, duplex, description, physicalAddress, mtu, txPower, forwardingModel.
Для интерфейсов с forwardingModel=bridged дополнительно: show interfaces <name> switchport configuration source | json
(поле switchportConfiguration: { "config": [...], "source": "cli" }).

Переменные: NETBOX_URL, NETBOX_TOKEN, SSH_USERNAME, SSH_PASSWORD, SSH_HOST_SUFFIX,
PARALLEL_DEVICES, NETBOX_TAG (по умолчанию border).

Вывод: по умолчанию таблица; с ключом --json — один JSON.
Ключ --from-file FILE: не опрашивать по SSH, взять данные из JSON-файла (формат как при --json) и вывести таблицу или JSON.
"""

import argparse
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import paramiko
import pynetbox

from uplinks_report import (
    arista_cli_interface_name,
    extract_json,
    get_device_platform_name,
    is_arista_platform,
    parse_arista_uplinks,
)


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


def get_arista_uplink_stats(host, username, password, timeout=45, command_timeout=90, log=None):
    """
    Подключиться по SSH к Arista, получить список интерфейсов с "Uplink:" в description,
    для каждого выполнить show int X | json | no-more и show int X transceiver | json | no-more;
    для интерфейсов с forwardingModel=bridged дополнительно show int X switchport configuration source | json.
    Возврат: список словарей с полями name, mediaType, bandwidth, duplex, description,
    physicalAddress, mtu, txPower, forwardingModel и при bridged — switchportConfiguration.
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

    # Список uplink-интерфейсов
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
        # show interfaces <name> | json | no-more
        send("show interfaces {} | json | no-more\r\n".format(cli_name))
        time.sleep(0.2)
        if_data = read_until_json_and_prompt(channel, timeout=command_timeout)
        # show interfaces <name> transceiver | json | no-more
        send("show interfaces {} transceiver | json | no-more\r\n".format(cli_name))
        time.sleep(0.2)
        trans_data = read_until_json_and_prompt(channel, timeout=command_timeout)

        if_data = (if_data or {}).get("interfaces") or {}
        trans_data = (trans_data or {}).get("interfaces") or {}
        if_obj = if_data.get(iface_name) or {}
        trans_obj = trans_data.get(iface_name) or {}

        # Для bridged — дополнительно собираем switchport configuration source
        switchport_config = None
        if (if_obj.get("forwardingModel") or "").strip().lower() == "bridged":
            send("show interfaces {} switchport configuration source | json | no-more\r\n".format(cli_name))
            time.sleep(0.2)
            sw_data = read_until_json_and_prompt(channel, timeout=command_timeout)
            sw_interfaces = (sw_data or {}).get("interfaces") or {}
            sw_iface = sw_interfaces.get(iface_name) or {}
            if sw_iface:
                switchport_config = sw_iface  # {"config": [...], "source": "cli"}
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
    """Обработать одно устройство Arista: SSH + сбор stats по uplinks. Возврат (device_name, list_of_dicts | error)."""
    progress_print(device.name, "подключение и сбор uplink stats...")
    platform_name = get_device_platform_name(device, nb)
    if not is_arista_platform(platform_name):
        progress_print(device.name, "пропуск (не Arista): {}".format(platform_name or "нет платформы"))
        return device.name, None

    ssh_host = device.name + ssh_suffix
    log_cb = lambda msg: progress_print(device.name, msg)
    stats, err = get_arista_uplink_stats(
        ssh_host, ssh_user, ssh_pass, log=log_cb
    )
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
    desc_col_idx = 8  # description — ограничиваем длину
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


def main():
    parser = argparse.ArgumentParser(description="Сбор uplink-статистики Arista из Netbox")
    parser.add_argument("--json", action="store_true", help="Вывод в формате JSON (по умолчанию — таблица)")
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

    url = os.environ.get("NETBOX_URL")
    token = os.environ.get("NETBOX_TOKEN")
    if not url or not token:
        print("Задайте переменные NETBOX_URL и NETBOX_TOKEN")
        return 1

    ssh_user = os.environ.get("SSH_USERNAME", "admin")
    ssh_pass = os.environ.get("SSH_PASSWORD")
    ssh_suffix = os.environ.get("SSH_HOST_SUFFIX", ".3hc.io")
    netbox_tag = os.environ.get("NETBOX_TAG", "border")
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

    out = {
        "devices": {
            dev_name: payload for dev_name, payload in results.items()
        }
    }
    print("", flush=True)

    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print_table(out["devices"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
