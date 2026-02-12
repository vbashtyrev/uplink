#!/usr/bin/env python3
"""
Проверки Netbox по данным из dry-ssh.json.
Достаём устройства из Netbox по тегу; если передан файл — сверяем hostname'ы
(различия выводим, совпадения не пишем). Ключ --intname: проверка имён интерфейсов (файл vs Netbox) с поиском по вариантам.
Ключ --description: сверка поля description (файл vs Netbox).
Ключ --mediatype: сверка mediaType / type (файл/SSH vs Netbox).
Ключ --mt-ref [FILE]: сверка mediaType со справочником (файл с interface_types);
  данные из файла/SSH и из Netbox сверяются со списком value в справочнике (по умолчанию netbox_interface_types.json).
Ключ --show-change: показывать колонки «что подставим в Netbox» по выбранным ключам (mediatype → mtToSet, description → descToSet, bandwidth → speedToSet, duplex → dupToSet, mtu → mtuToSet, tx-power → txpToSet).
Ключ --apply: вносить изменения в Netbox при разнице по выбранным ключам (--mediatype, --description, --bandwidth, --duplex, --mtu, --tx-power).
  Справочник типов (--mt-ref) по умолчанию включён (netbox_interface_types.json); для типа в Netbox подставляется value/slug из справочника.

Переменные: NETBOX_URL, NETBOX_TOKEN, NETBOX_TAG (по умолчанию border).

По умолчанию таблица; с --json — JSON. В note — код замечания, расшифровка под таблицей.
"""

import argparse
import json
import os
import re
import sys

import pynetbox


def _get_device_platform_name(device, nb):
    """Имя платформы из NetBox (device.platform.name) для определения Juniper/Arista."""
    pl = getattr(device, "platform", None)
    if pl is None:
        return None
    if hasattr(pl, "name"):
        return getattr(pl, "name", None)
    if isinstance(pl, int):
        try:
            p = nb.dcim.platforms.get(pl)
            return getattr(p, "name", None) if p else None
        except Exception:
            return None
    return None


def _is_arista_platform(platform_name):
    """По имени платформы из NetBox: Arista EOS → True."""
    if not platform_name:
        return False
    n = platform_name.lower()
    return "arista" in n or "eos" in n


def _is_juniper_platform(platform_name):
    """По имени платформы из NetBox: JunOS / Juniper → True."""
    if not platform_name:
        return False
    n = platform_name.lower()
    return "junos" in n or "juniper" in n


def load_file(path):
    """Загрузить JSON из файла. Возврат (data, None) или (None, error_msg)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "файл не найден: {}".format(path)
    except json.JSONDecodeError as e:
        return None, "ошибка JSON: {}".format(e)
    if "devices" not in data:
        return None, "в файле нет ключа 'devices'"
    return data, None


def interface_name_variants(name):
    """
    Сгенерировать варианты имени интерфейса для поиска в Netbox.
    Исходный вид: Ethernet51/1. Варианты:
    - с маленькой буквы: ethernet51/1
    - сокращения с /: Eth51/1, eth51/1, Et51/1, et51/1
    - без слэша и цифры после: Ethernet51, ethernet51, Eth51, eth51, Et51, et51
    """
    if not name or not name.strip():
        return []
    name = name.strip()
    # Разбить на префикс (Ethernet/Eth/Et) и числовую часть (51/1 или 51)
    m = re.match(r"^([A-Za-z]+)(\d+(?:/\d+)?)$", name)
    if not m:
        return [name, name.lower()]
    prefix, num_part = m.group(1), m.group(2)
    num_no_slash = num_part.split("/")[0] if "/" in num_part else num_part

    prefixes = []
    pl = prefix.lower()
    if pl.startswith("ethernet"):
        prefixes = ["Ethernet", "ethernet", "Eth", "eth", "Et", "et"]
    elif pl.startswith("eth"):
        prefixes = ["Eth", "eth", "Et", "et"]
    elif pl.startswith("et"):
        prefixes = ["Et", "et"]
    else:
        prefixes = [prefix, pl]

    variants = []
    for p in prefixes:
        variants.append(p + num_part)       # с слэшем
        variants.append(p + num_no_slash)  # без слэша
    # Убрать дубликаты, сохранить порядок; первым оставить точное совпадение
    seen = set()
    result = []
    for v in [name] + variants:
        if v not in seen:
            seen.add(v)
            result.append(v)
    return result


def compare_hostnames(file_devices, netbox_names):
    """Вернуть (only_in_file, only_in_netbox)."""
    file_set = set(file_devices)
    nb_set = set(netbox_names)
    return sorted(file_set - nb_set), sorted(nb_set - file_set)


# Коды примечаний для note (расшифровка — в NOTE_LEGEND)
NOTE_OK = ""
NOTE_ALT = 1   # альтернативное написание (eth51/1 и т.п.)
NOTE_LOWER = 2 # имя в нижнем регистре
NOTE_NO_SLASH = 3  # без слэша и номера после
NOTE_MISSING = 4    # не найден в Netbox

NOTE_LEGEND = {
    NOTE_ALT: "в Netbox найден как другой вариант: альтернативное написание (например 'eth51/1')",
    NOTE_LOWER: "в Netbox найден как другой вариант: имя в нижнем регистре",
    NOTE_NO_SLASH: "в Netbox найден как другой вариант: без слэша и номера после (например 'eth49')",
    NOTE_MISSING: "в Netbox не найден (ни один вариант)",
}


def check_intname(device_name, int_name, nb_interfaces_by_name):
    """
    Проверить, есть ли интерфейс с именем int_name у устройства.
    nb_interfaces_by_name — dict: name (как в Netbox) -> interface object.
    Возврат: (status, nb_name, note_code).
    status: "ok" | "found" | "missing"
    nb_name: имя в Netbox (при ok = int_name, при found = найденный вариант, при missing = "")
    note_code: NOTE_OK | 1..4
    """
    if int_name in nb_interfaces_by_name:
        return "ok", int_name, NOTE_OK
    variants = interface_name_variants(int_name)
    for v in variants[1:]:
        if v in nb_interfaces_by_name:
            if v == int_name.lower():
                return "found", v, NOTE_LOWER
            if "/" not in v and "/" in int_name:
                return "found", v, NOTE_NO_SLASH
            return "found", v, NOTE_ALT
    return "missing", "", NOTE_MISSING


def resolve_interface(int_name, nb_interfaces_by_name):
    """
    Найти интерфейс в Netbox по имени (точное или вариант).
    Возврат: (nb_name, iface_object | None). Если не найден — ("", None).
    """
    if int_name in nb_interfaces_by_name:
        return int_name, nb_interfaces_by_name[int_name]
    for v in interface_name_variants(int_name)[1:]:
        if v in nb_interfaces_by_name:
            return v, nb_interfaces_by_name[v]
    return "", None


# Коды примечаний для description (5–6)
DESC_NOTE_DIFF = 5   # описание различается
DESC_NOTE_NO_INT = 6 # интерфейс не найден в Netbox

# Коды для mediaType (7–9)
MT_NOTE_DIFF = 7       # mediaType (из dry-ssh) и type в Netbox различаются по справочнику типов
MT_NOTE_F_NOT_IN_REF = 8  # тип из dry-ssh не найден в справочнике типов (netbox_interface_types.json)
MT_NOTE_N_NOT_IN_REF = 9  # тип в Netbox не найден в справочнике типов

# Код для bandwidth/speed (10)
BW_NOTE_DIFF = 10      # bandwidth (dry-ssh) и speed (Netbox) различаются

# Код для duplex (11)
DUP_NOTE_DIFF = 11     # duplex из dry-ssh и duplex в Netbox различаются

# Код для MAC / physicalAddress (12)
MAC_NOTE_DIFF = 12     # physicalAddress (dry-ssh) и mac (Netbox) различаются
MAC_NOTE_NOT_BOTH = 16  # в Netbox заполнено не оба поля: mac_address и mac_addresses

# Код для MTU (13)
MTU_NOTE_DIFF = 13     # mtu из dry-ssh и mtu в Netbox различаются

# Код для tx_power (14)
TXPOWER_NOTE_DIFF = 14  # txPower (dry-ssh) и tx_power (Netbox) различаются

# Код для forwardingModel / mode (15)
FWD_NOTE_DIFF = 15     # forwardingModel (dry-ssh) и mode (Netbox) различаются

# Общая легенда для одной таблицы
ALL_LEGEND = {
    **NOTE_LEGEND,
    DESC_NOTE_DIFF: "описание в файле и в Netbox различается",
    DESC_NOTE_NO_INT: "интерфейс не найден в Netbox",
    MT_NOTE_DIFF: "mediaType из dry-ssh и type в Netbox различаются (по value из справочника типов)",
    MT_NOTE_F_NOT_IN_REF: "тип из dry-ssh не найден в справочнике типов (netbox_interface_types.json)",
    MT_NOTE_N_NOT_IN_REF: "тип в Netbox не найден в справочнике типов (netbox_interface_types.json)",
    BW_NOTE_DIFF: "bandwidth (dry-ssh, bps) и speed (Netbox, Kbps) различаются после приведения к bps",
    DUP_NOTE_DIFF: "duplex из dry-ssh и duplex в Netbox различаются",
    MAC_NOTE_DIFF: "physicalAddress (dry-ssh) и mac (Netbox) различаются",
    MAC_NOTE_NOT_BOTH: "в Netbox заполнено не оба поля: mac_address на интерфейсе и сущность dcim.mac-addresses",
    MTU_NOTE_DIFF: "mtu из dry-ssh и mtu в Netbox различаются",
    TXPOWER_NOTE_DIFF: "txPower (dry-ssh) и tx_power (Netbox) различаются",
    FWD_NOTE_DIFF: "forwardingModel (dry-ssh) и mode (Netbox) различаются",
}


def _normalize_duplex(val):
    """Привести duplex к 'full' или 'half' для сравнения. Arista: duplexFull/duplexHalf, Netbox: full/Full."""
    if val is None:
        return ""
    s = str(val).strip().lower()
    if not s:
        return ""
    if "full" in s or s == "full":
        return "full"
    if "half" in s or s == "half":
        return "half"
    return s


def _fwd_file_to_netbox_mode(fwd_f):
    """Значение mode для Netbox по forwardingModel из файла: routed → None, bridged → tagged."""
    if not fwd_f:
        return None
    s = str(fwd_f).strip().lower()
    if s == "routed":
        return None
    if s == "bridged":
        return "tagged"
    return str(fwd_f).strip()


def _normalize_mac(val):
    """Привести MAC к виду с двоеточиями, нижний регистр (для сравнения physicalAddress и mac_address)."""
    if val is None:
        return ""
    s = str(val).strip().lower().replace("-", ":").replace(".", ":")
    return s


def _mac_netbox_format(val):
    """Формат MAC для NetBox API: двоеточия, верхний регистр (44:4C:A8:BF:2E:91)."""
    n = _normalize_mac(val)
    return n.upper() if n else ""


def _get_interface_mac(nb_iface):
    """
    Получить MAC интерфейса из NetBox. В новых версиях MAC хранится в сущности
    dcim.mac-addresses, интерфейс имеет mac_address (может быть null) и mac_addresses (список).
    """
    if nb_iface is None:
        return ""
    direct = getattr(nb_iface, "mac_address", None)
    if direct:
        return str(direct).strip()
    addrs = getattr(nb_iface, "mac_addresses", None)
    if addrs and len(addrs) > 0:
        first = addrs[0]
        if isinstance(first, dict):
            return str(first.get("mac_address") or first.get("display") or "").strip()
        return str(getattr(first, "mac_address", None) or getattr(first, "display", "") or "").strip()
    return ""


def _mac_both_filled(nb_iface):
    """Проверить, что в Netbox заполнены оба: отображение MAC на интерфейсе (mac_address или primary_mac_address) и список mac_addresses."""
    if nb_iface is None:
        return False
    direct = getattr(nb_iface, "mac_address", None)
    primary = getattr(nb_iface, "primary_mac_address", None)
    display_ok = bool(direct and str(direct).strip()) or bool(primary)
    addrs = getattr(nb_iface, "mac_addresses", None)
    list_ok = bool(addrs and len(addrs) > 0)
    return display_ok and list_ok


def load_mt_ref(path):
    """
    Загрузить справочник типов интерфейсов из JSON (формат netbox_interface_types.json).
    Возврат: (ref_values_set, ref_list, None) или (None, None, error_msg).
    ref_list — список dict с value/label для проверки по label (файл/SSH часто отдаёт label, не value).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, None, "файл не найден: {}".format(path)
    except json.JSONDecodeError as e:
        return None, None, "ошибка JSON: {}".format(e)
    types_list = data.get("interface_types") if isinstance(data, dict) else data
    if not isinstance(types_list, list):
        return None, None, "в файле ожидается ключ 'interface_types' (список)"
    values = set()
    for item in types_list:
        if isinstance(item, dict) and item.get("value"):
            values.add(str(item.get("value")).strip())
        elif isinstance(item, str):
            values.add(item.strip())
    return values, types_list, None


def _mt_in_ref(mt_str, ref_values, ref_list):
    """Проверить, что тип mt_str есть в справочнике (по value или label)."""
    if not mt_str:
        return True
    if not ref_values:
        return False
    canonical = _mt_to_value(mt_str, ref_values, ref_list or [])
    return canonical in ref_values


def _mt_to_value(mt_str, ref_values, ref_list):
    """
    Привести тип (value или label из файла/SSH/Netbox) к каноническому value по справочнику.
    Возврат: value из справочника или исходная строка, если не найдено.
    """
    if not mt_str or not ref_list:
        return (mt_str or "").strip()
    mt = (mt_str or "").strip()
    if mt in ref_values:
        return mt
    for item in ref_list:
        if not isinstance(item, dict):
            continue
        val = (item.get("value") or "").strip()
        label = (item.get("label") or "").strip()
        if val and mt == val:
            return val
        if label and (mt == label or label.startswith(mt) or mt.startswith(label)):
            return val
    return mt


def _netbox_type_to_str(nb_iface):
    """Взять тип интерфейса из объекта Netbox (type.value или type.label)."""
    t = getattr(nb_iface, "type", None)
    if t is None:
        return ""
    if isinstance(t, dict):
        out = str(t.get("value") or t.get("label") or "").strip()
    else:
        out = str(getattr(t, "value", None) or getattr(t, "label", None) or t or "").strip()
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Проверки Netbox по данным из JSON (файл/SSH). Сверка полей интерфейсов, при --apply — обновление в Netbox.",
    )
    # --- Входные данные ---
    g_in = parser.add_argument_group("Входные данные (файл, хост, платформа)")
    g_in.add_argument(
        "--file", "-f",
        default="dry-ssh.json",
        metavar="FILE",
        help="JSON с устройствами и интерфейсами (по умолчанию dry-ssh.json)",
    )
    g_in.add_argument(
        "--host",
        metavar="NAME",
        help="Обработать только один хост (имя устройства)",
    )
    g_in.add_argument(
        "--platform",
        choices=("arista", "juniper", "all"),
        default="arista",
        help="Платформа в NetBox: arista (по умолчанию), juniper или all",
    )
    # --- Проверки (какие поля сверять) ---
    g_checks = parser.add_argument_group("Проверки (какие поля сверять)")
    g_checks.add_argument(
        "--intname",
        action="store_true",
        help="Имена интерфейсов (файл vs Netbox) с поиском по вариантам",
    )
    g_checks.add_argument(
        "--description",
        action="store_true",
        help="Поле description",
    )
    g_checks.add_argument(
        "--mediatype",
        action="store_true",
        help="mediaType (файл) / type (Netbox)",
    )
    g_checks.add_argument(
        "--mt-ref",
        nargs="?",
        const="netbox_interface_types.json",
        default="netbox_interface_types.json",
        metavar="FILE",
        help="Справочник типов для mediaType (по умолчанию netbox_interface_types.json)",
    )
    g_checks.add_argument(
        "--no-mt-ref",
        action="store_true",
        dest="no_mt_ref",
        help="Не загружать справочник типов",
    )
    g_checks.add_argument(
        "--bandwidth",
        action="store_true",
        help="bandwidth (файл) / speed (Netbox)",
    )
    g_checks.add_argument(
        "--duplex",
        action="store_true",
        help="duplex",
    )
    g_checks.add_argument(
        "--mac",
        action="store_true",
        help="physicalAddress (файл) / mac_address (Netbox)",
    )
    g_checks.add_argument(
        "--mtu",
        action="store_true",
        help="mtu",
    )
    g_checks.add_argument(
        "--tx-power",
        action="store_true",
        dest="tx_power",
        help="txPower (файл) / tx_power (Netbox)",
    )
    g_checks.add_argument(
        "--forwarding-model",
        action="store_true",
        dest="forwarding_model",
        help="forwardingModel (файл) / mode (Netbox) — режим работы порта",
    )
    g_checks.add_argument(
        "--all",
        action="store_true",
        dest="all_checks",
        help="Включить все проверки сразу",
    )
    # --- Вывод ---
    g_out = parser.add_argument_group("Вывод")
    g_out.add_argument(
        "--show-change",
        action="store_true",
        dest="show_change",
        help="Колонки «что подставим в Netbox» по выбранным ключам (mtToSet, descToSet, …)",
    )
    g_out.add_argument(
        "--hide-empty-note-cols",
        action="store_true",
        dest="hide_empty_note_cols",
        help="Не выводить колонки примечаний (nD, nM, …), если во всех строках пусто",
    )
    g_out.add_argument(
        "--hide-no-diff-cols",
        action="store_true",
        dest="hide_no_diff_cols",
        help="Не выводить группы колонок (файл/Netbox/примечание), в которых ни в одной строке нет расхождения",
    )
    g_out.add_argument(
        "--json",
        action="store_true",
        help="Вывод в JSON (по умолчанию — таблица)",
    )
    # --- Применение в Netbox ---
    g_apply = parser.add_argument_group("Применение в Netbox")
    g_apply.add_argument(
        "--apply",
        action="store_true",
        dest="apply",
        help="При разнице по выбранным ключам обновлять интерфейс в Netbox (при этом таблица не выводится)",
    )
    args = parser.parse_args()

    if args.all_checks:
        args.intname = True
        args.description = True
        args.mediatype = True
        args.bandwidth = True
        args.duplex = True
        args.mac = True
        args.mtu = True
        args.tx_power = True
        args.forwarding_model = True
    if args.no_mt_ref:
        args.mt_ref = None

    url = os.environ.get("NETBOX_URL")
    token = os.environ.get("NETBOX_TOKEN")
    netbox_tag = os.environ.get("NETBOX_TAG", "border")
    if not url or not token:
        print("Задайте NETBOX_URL и NETBOX_TOKEN")
        return 1

    data, err = load_file(args.file)
    if err:
        print(err)
        return 1

    if args.host:
        if args.host not in data["devices"]:
            print("Хост '{}' не найден в файле.".format(args.host), file=sys.stderr)
            return 1
        data["devices"] = {args.host: data["devices"][args.host]}

    file_devices = list(data["devices"].keys())
    nb = pynetbox.api(url, token=token)
    nb_devices = list(nb.dcim.devices.filter(tag=netbox_tag))
    nb_names = [d.name for d in nb_devices]
    nb_by_name = {d.name: d for d in nb_devices}

    # Сверка hostname'ов (при --host не выводим: список «только в Netbox» не нужен)
    only_file, only_nb = compare_hostnames(file_devices, nb_names)
    if args.platform != "all" and only_nb:
        only_nb = [
            n for n in only_nb
            if n in nb_by_name
            and (
                (args.platform == "arista" and _is_arista_platform(_get_device_platform_name(nb_by_name[n], nb)))
                or (args.platform == "juniper" and _is_juniper_platform(_get_device_platform_name(nb_by_name[n], nb)))
            )
        ]
    if not args.host and (only_file or only_nb):
        if only_file:
            print("Только в файле (нет в Netbox по тегу {}): {}".format(netbox_tag, ", ".join(only_file)))
        if only_nb:
            print("Только в Netbox (нет в файле): {}".format(", ".join(only_nb)))
        print()

    mt_ref_values = None
    mt_ref_list = None
    if args.mediatype and args.mt_ref:
        mt_ref_values, mt_ref_list, ref_err = load_mt_ref(args.mt_ref)
        if ref_err:
            print("Справочник типов (--mt-ref): {}.".format(ref_err), flush=True)
        elif mt_ref_values:
            print("Справочник типов загружен: {} записей ({}).".format(len(mt_ref_values), args.mt_ref), flush=True)

    if args.mediatype and mt_ref_values is None:
        print("Внимание: без справочника типов (--mt-ref) значения mediaType не приводятся к одному формату, "
              "сверка может показывать расхождения из-за разного написания (файл vs Netbox).", file=sys.stderr, flush=True)

    out = {}
    if args.intname or args.description or args.mediatype or (args.show_change and mt_ref_values) or args.bandwidth or args.duplex or args.mac or args.mtu or args.tx_power or args.forwarding_model:
        # Один проход: строки (..., mtToSet=10, ...); индексы 26-30 — *ToSet при --show-change
        rows = []
        note_codes_used = set()
        skipped_no_netbox = []
        skipped_not_list = []
        skipped_platform = []
        for dev_name in sorted(data["devices"].keys()):
            payload = data["devices"][dev_name]
            if not isinstance(payload, list):
                skipped_not_list.append(dev_name)
                continue
            device = nb_by_name.get(dev_name)
            if not device:
                skipped_no_netbox.append(dev_name)
                continue
            if args.platform != "all":
                platform_name = _get_device_platform_name(device, nb)
                if args.platform == "arista" and not _is_arista_platform(platform_name):
                    skipped_platform.append(dev_name)
                    continue
                if args.platform == "juniper" and not _is_juniper_platform(platform_name):
                    skipped_platform.append(dev_name)
                    continue
            nb_ifaces = list(nb.dcim.interfaces.filter(device_id=device.id))
            nb_by_iface_name = {iface.name: iface for iface in nb_ifaces}
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                int_name = entry.get("name")
                if not int_name:
                    continue
                nb_name, nb_iface = resolve_interface(int_name, nb_by_iface_name)
                note = ""
                if args.intname:
                    _, _, note_code = check_intname(dev_name, int_name, nb_by_iface_name)
                    note = note_code if note_code != NOTE_OK else ""
                    if note_code != NOTE_OK:
                        note_codes_used.add(note_code)
                desc_f = (entry.get("description") or "").strip() if args.description else ""
                desc_n = ""
                nD = ""
                if args.description:
                    if nb_iface is None:
                        nD = DESC_NOTE_NO_INT
                        note_codes_used.add(DESC_NOTE_NO_INT)
                    else:
                        desc_n = (getattr(nb_iface, "description", None) or "").strip()
                        if desc_f != desc_n:
                            nD = DESC_NOTE_DIFF
                            note_codes_used.add(DESC_NOTE_DIFF)
                mt_f = (entry.get("mediaType") or "").strip() if (args.mediatype or args.show_change) else ""
                mt_n = ""
                nM = ""
                mt_to_set = ""
                if args.mediatype or args.show_change:
                    if nb_iface is not None:
                        mt_n = _netbox_type_to_str(nb_iface)
                    ref_list = mt_ref_list or []
                    mt_f_value = _mt_to_value(mt_f, mt_ref_values or set(), ref_list) if mt_ref_values else mt_f
                    mt_n_value = _mt_to_value(mt_n, mt_ref_values or set(), ref_list) if mt_ref_values else mt_n
                    # slug для Netbox (value из справочника) — для вывода mtToSet и для --apply
                    if mt_ref_values and mt_f_value:
                        mt_to_set = mt_f_value
                    if args.mediatype and nb_iface is not None:
                        if mt_ref_values:
                            if mt_f_value and mt_n_value and mt_f_value != mt_n_value:
                                nM = str(MT_NOTE_DIFF)
                                note_codes_used.add(MT_NOTE_DIFF)
                        else:
                            if mt_f != mt_n:
                                nM = str(MT_NOTE_DIFF)
                                note_codes_used.add(MT_NOTE_DIFF)
                    if mt_ref_values is not None:
                        n_codes = []
                        if mt_f and not _mt_in_ref(mt_f, mt_ref_values, ref_list):
                            n_codes.append(str(MT_NOTE_F_NOT_IN_REF))
                            note_codes_used.add(MT_NOTE_F_NOT_IN_REF)
                        if mt_n and mt_n not in mt_ref_values:
                            n_codes.append(str(MT_NOTE_N_NOT_IN_REF))
                            note_codes_used.add(MT_NOTE_N_NOT_IN_REF)
                        if n_codes:
                            nM = (nM + "," if nM else "") + ",".join(n_codes)
                bw_f = None
                speed_n = None
                nB = ""
                if args.bandwidth:
                    # bandwidth из dry-ssh — в bps (битах/с), speed из Netbox — в Kbps
                    bw_f = entry.get("bandwidth")
                    if bw_f is not None and not isinstance(bw_f, (int, float)):
                        try:
                            bw_f = int(bw_f)
                        except (TypeError, ValueError):
                            pass
                    if nb_iface is not None:
                        speed_n = getattr(nb_iface, "speed", None)
                        if speed_n is not None and not isinstance(speed_n, (int, float)):
                            try:
                                speed_n = int(speed_n)
                            except (TypeError, ValueError):
                                pass
                    if bw_f is not None:
                        # Показываем разницу, если в Netbox нет speed или он не совпадает с файлом
                        if speed_n is None:
                            nB = str(BW_NOTE_DIFF)
                            note_codes_used.add(BW_NOTE_DIFF)
                        else:
                            speed_n_bps = speed_n * 1000  # Netbox speed в Kbps -> bps
                            if bw_f != speed_n_bps:
                                nB = str(BW_NOTE_DIFF)
                                note_codes_used.add(BW_NOTE_DIFF)
                dup_f = ""
                dup_n = ""
                nDup = ""
                if args.duplex:
                    dup_f_raw = entry.get("duplex")
                    dup_f = str(dup_f_raw or "").strip() if dup_f_raw is not None else ""
                    if nb_iface is not None:
                        d = getattr(nb_iface, "duplex", None)
                        if isinstance(d, dict):
                            dup_n = str(d.get("value") or d.get("label") or "").strip()
                        else:
                            dup_n = str(d or "").strip()
                    dup_f_norm = _normalize_duplex(dup_f)
                    dup_n_norm = _normalize_duplex(dup_n)
                    if dup_f_norm and dup_n_norm and dup_f_norm != dup_n_norm:
                        nDup = str(DUP_NOTE_DIFF)
                        note_codes_used.add(DUP_NOTE_DIFF)
                    # в таблице показываем нормализованные значения (full/half), чтобы duplexFull и Full не выглядели по-разному
                    dup_f_out = dup_f_norm if dup_f_norm else dup_f
                    dup_n_out = dup_n_norm if dup_n_norm else dup_n
                else:
                    dup_f_out = ""
                    dup_n_out = ""
                mac_f = ""
                mac_n = ""
                nMac = ""
                if args.mac:
                    mac_f_raw = entry.get("physicalAddress")
                    mac_f = str(mac_f_raw or "").strip() if mac_f_raw is not None else ""
                    if nb_iface is not None:
                        mac_n = _get_interface_mac(nb_iface)
                    mac_f_norm = _normalize_mac(mac_f)
                    mac_n_norm = _normalize_mac(mac_n)
                    if mac_f_norm and (not mac_n_norm or mac_f_norm != mac_n_norm):
                        nMac = str(MAC_NOTE_DIFF)
                        note_codes_used.add(MAC_NOTE_DIFF)
                    if args.mac and nb_iface and mac_n_norm and not _mac_both_filled(nb_iface):
                        nMac = (nMac + "," if nMac else "") + str(MAC_NOTE_NOT_BOTH)
                        note_codes_used.add(MAC_NOTE_NOT_BOTH)
                mtu_f = ""
                mtu_n = ""
                nMtu = ""
                if args.mtu:
                    mtu_f_raw = entry.get("mtu")
                    if mtu_f_raw is not None:
                        try:
                            mtu_f = int(mtu_f_raw)
                        except (TypeError, ValueError):
                            mtu_f = str(mtu_f_raw).strip()
                    if nb_iface is not None:
                        mtu_n_raw = getattr(nb_iface, "mtu", None)
                        if mtu_n_raw is not None:
                            try:
                                mtu_n = int(mtu_n_raw)
                            except (TypeError, ValueError):
                                mtu_n = str(mtu_n_raw).strip()
                    if mtu_f != "" and (mtu_n == "" or mtu_f != mtu_n):
                        nMtu = str(MTU_NOTE_DIFF)
                        note_codes_used.add(MTU_NOTE_DIFF)
                txp_f = ""
                txp_n = ""
                nTxp = ""
                if args.tx_power:
                    txp_f_raw = entry.get("txPower")
                    if txp_f_raw is not None:
                        try:
                            txp_f = int(round(float(txp_f_raw)))
                        except (TypeError, ValueError):
                            txp_f = str(txp_f_raw).strip()
                    if nb_iface is not None:
                        txp_n_raw = getattr(nb_iface, "tx_power", None)
                        if txp_n_raw is not None:
                            try:
                                txp_n = int(round(float(txp_n_raw)))
                            except (TypeError, ValueError):
                                txp_n = str(txp_n_raw).strip()
                    if txp_f != "" and (txp_n == "" or txp_f != txp_n):
                        nTxp = str(TXPOWER_NOTE_DIFF)
                        note_codes_used.add(TXPOWER_NOTE_DIFF)
                # вывод tx_power целым числом, без точки
                txp_f_out = str(int(txp_f)) if isinstance(txp_f, (int, float)) else txp_f
                txp_n_out = str(int(txp_n)) if isinstance(txp_n, (int, float)) else txp_n
                fwd_f = ""
                fwd_n = ""
                nFwd = ""
                if args.forwarding_model:
                    fwd_f = str(entry.get("forwardingModel") or "").strip()
                    if nb_iface is not None:
                        mode_raw = getattr(nb_iface, "mode", None)
                        if isinstance(mode_raw, dict):
                            fwd_n = str(mode_raw.get("value") or mode_raw.get("label") or "").strip()
                        else:
                            fwd_n = str(mode_raw or "").strip()
                    # В Netbox: routed → mode=null, bridged → mode=tagged; сравнение по значению для Netbox
                    fwd_f_cmp = (_fwd_file_to_netbox_mode(fwd_f) or "")
                    fwd_n_cmp = (fwd_n or "").strip().lower()
                    if fwd_f_cmp != fwd_n_cmp:
                        nFwd = str(FWD_NOTE_DIFF)
                        note_codes_used.add(FWD_NOTE_DIFF)
                fwd_to_set = (_fwd_file_to_netbox_mode(fwd_f) or "") if (args.show_change and args.forwarding_model and fwd_f) else ""
                # значения «что подставим» для --show-change (индексы 26–30)
                desc_to_set = desc_f if (args.show_change and args.description) else ""
                speed_to_set = (bw_f // 1000) if (args.show_change and args.bandwidth and bw_f is not None) else ""
                dup_to_set = (_normalize_duplex(dup_f) or dup_f) if (args.show_change and args.duplex and dup_f) else ""
                mtu_to_set = mtu_f if (args.show_change and args.mtu and mtu_f != "") else ""
                txp_to_set = txp_f_out if (args.show_change and args.tx_power) else ""
                # --apply: вносить изменения в Netbox по выбранным ключам при разнице
                if args.apply and nb_iface is not None:
                    updates = {}
                    if args.intname and (nb_name or "").strip() != (int_name or "").strip():
                        updates["name"] = int_name
                    if args.description and nD:
                        updates["description"] = desc_f
                    if args.mediatype and nM:
                        updates["type"] = mt_to_set if mt_to_set else mt_f
                    if args.bandwidth and bw_f is not None and (speed_n is None or (bw_f // 1000) != speed_n):
                        updates["speed"] = bw_f // 1000  # bps -> Kbps
                    if args.duplex and dup_f:
                        dup_val = _normalize_duplex(dup_f) or dup_f
                        if dup_val and (dup_n or "").strip() != dup_val:
                            updates["duplex"] = dup_val
                    if args.mtu and mtu_f != "" and (mtu_n == "" or mtu_f != mtu_n):
                        updates["mtu"] = int(mtu_f) if isinstance(mtu_f, (int, float)) else mtu_f
                    if args.tx_power and txp_f != "" and isinstance(txp_f, (int, float)) and (txp_n == "" or txp_f != txp_n):
                        updates["tx_power"] = txp_f
                    if args.forwarding_model and nFwd and fwd_f:
                        # В Netbox: routed → mode=null, bridged → mode=tagged
                        updates["mode"] = _fwd_file_to_netbox_mode(fwd_f)
                    if updates:
                        try:
                            nb_iface.update(updates)
                            print("Обновлено {} {}: {}".format(dev_name, nb_name or int_name, list(updates.keys())), flush=True)
                        except Exception as e:
                            print("Ошибка обновления {} {}: {} — {}".format(dev_name, nb_name or int_name, updates, e), file=sys.stderr, flush=True)
                    # MAC в Netbox — отдельная сущность (dcim.mac-addresses); затем на интерфейсе ставим primary_mac_address
                    if args.mac and (nMac or (mac_f and not mac_n)) and nb_iface is not None:
                        mac_netbox = _mac_netbox_format(mac_f)
                        if not mac_netbox:
                            pass
                        else:
                            try:
                                existing = list(nb.dcim.mac_addresses.filter(mac_address=mac_netbox))
                                if existing:
                                    rec = existing[0]
                                    url = getattr(rec, "url", None) or getattr(rec, "display", rec)
                                    print("MAC {} {} {}: уже в Netbox — {}".format(dev_name, nb_name or int_name, mac_netbox, url), flush=True)
                                else:
                                    created = nb.dcim.mac_addresses.create(
                                        mac_address=mac_netbox,
                                        assigned_object_type="dcim.interface",
                                        assigned_object_id=nb_iface.id,
                                    )
                                    rec = created
                                    url = getattr(created, "url", None) or getattr(created, "display", created)
                                    print("MAC {} {} {}: создан — {}".format(dev_name, nb_name or int_name, mac_netbox, url), flush=True)
                                # Заполнить поле интерфейса: в NetBox 4 это primary_mac_address (ID записи MAC)
                                mac_id = getattr(rec, "id", None)
                                if mac_id is not None:
                                    try:
                                        nb_iface.update({"primary_mac_address": mac_id})
                                        print("Обновлено {} {}: primary_mac_address={}".format(dev_name, nb_name or int_name, mac_id), flush=True)
                                    except Exception as e2:
                                        print("Ошибка установки primary_mac_address {} {}: {} — {}".format(dev_name, nb_name or int_name, mac_id, e2), file=sys.stderr, flush=True)
                            except Exception as e:
                                print("Ошибка MAC {} {} {}: {} — {}".format(dev_name, nb_name or int_name, mac_netbox, e), file=sys.stderr, flush=True)
                rows.append((dev_name, int_name, nb_name, note, desc_f, desc_n, nD, mt_f, mt_n, nM, mt_to_set, bw_f, speed_n, nB, dup_f_out, dup_n_out, nDup, mac_f, mac_n, nMac, mtu_f, mtu_n, nMtu, txp_f_out, txp_n_out, nTxp, desc_to_set, speed_to_set, dup_to_set, mtu_to_set, txp_to_set, fwd_f, fwd_n, nFwd, fwd_to_set))
        if skipped_no_netbox:
            print("Пропущено (устройство есть в файле, но нет в Netbox по тегу): {}.".format(", ".join(skipped_no_netbox)))
        if skipped_not_list:
            print("Пропущено (в файле не список интерфейсов): {}.".format(", ".join(skipped_not_list)))
        if skipped_platform:
            print("Пропущено (платформа не {}): {}.".format(args.platform, ", ".join(skipped_platform)))
        if not args.apply:
            col_spec = _build_col_spec(args)
            if args.hide_empty_note_cols and rows:
                col_spec = _filter_empty_note_cols(col_spec, rows)
            if args.hide_no_diff_cols and rows:
                col_spec = _filter_no_diff_cols(col_spec, rows)
            if args.json:
                out["rows"] = [
                    _row_to_dict(r, col_spec)
                    for r in rows
                ]
                out["note_legend"] = {str(k): v for k, v in ALL_LEGEND.items() if k in note_codes_used}
            else:
                _print_combined_table(rows, note_codes_used, col_spec)
        else:
            print("При --apply таблица не выводится (данные уже обновлены в Netbox).", flush=True)

    if args.json and out:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


# Заголовки колонок-примечаний (nD, nM, nB, …) — для --hide-empty-note-cols
NOTE_COL_HEADERS = {"nD", "nM", "nB", "nDup", "nMac", "nMtu", "nTxp", "nFwd"}

# Группы колонок по проверке: при пустом примечании во всех строках скрываем всю группу (--hide-no-diff-cols)
DIFF_GROUPS_BY_NOTE = {
    "nD": {"descF", "descN", "nD", "descToSet"},
    "nM": {"mtF", "mtN", "nM", "mtToSet"},
    "nB": {"bwF", "speedN", "nB", "speedToSet"},
    "nDup": {"dupF", "dupN", "nDup", "dupToSet"},
    "nMac": {"macF", "macN", "nMac"},
    "nMtu": {"mtuF", "mtuN", "nMtu", "mtuToSet"},
    "nTxp": {"txpF", "txpN", "nTxp", "txpToSet"},
    "nFwd": {"fwdF", "fwdN", "nFwd", "fwdToSet"},
}


def _filter_empty_note_cols(col_spec, rows):
    """Убрать из col_spec колонки примечаний, у которых во всех строках пусто."""
    def keep(col):
        header, idx, _ = col
        if header not in NOTE_COL_HEADERS:
            return True
        return any(idx < len(r) and r[idx] for r in rows)
    return [c for c in col_spec if keep(c)]


def _filter_no_diff_cols(col_spec, rows):
    """Убрать группы колонок (файл/Netbox/примечание), где во всех строках примечание пусто — расхождений нет."""
    headers_in_spec = {c[0] for c in col_spec}
    cols_to_remove = set()
    for note, group in DIFF_GROUPS_BY_NOTE.items():
        if note not in headers_in_spec:
            continue
        note_col = next((c for c in col_spec if c[0] == note), None)
        if not note_col:
            continue
        _, note_idx, _ = note_col
        if all(note_idx >= len(r) or not r[note_idx] for r in rows):
            cols_to_remove |= (group & headers_in_spec)
    return [c for c in col_spec if c[0] not in cols_to_remove]


def _build_col_spec(args):
    """
    Список колонок по переданным ключам. Элемент: (header, row_index, max_width).
    row: name=0..intN=2, note=3, descF=4,descN=5,nD=6, mtF=7..mtToSet=10, bwF=11,speedN=12,nB=13, dupF=14,dupN=15,nDup=16, macF=17,macN=18,nMac=19, mtuF=20,mtuN=21,nMtu=22, txpF=23,txpN=24,nTxp=25, descToSet=26..txpToSet=30, fwdF=31,fwdN=32,nFwd=33,fwdToSet=34.
    """
    max_desc_len = 50
    cols = [("name", 0, 999), ("intF", 1, 999), ("intN", 2, 999)]
    if args.intname:
        cols.append(("note", 3, 999))
    if args.description:
        cols.extend([("descF", 4, max_desc_len + 3), ("descN", 5, max_desc_len + 3), ("nD", 6, 999)])
    if args.mediatype or args.show_change:
        cols.extend([("mtF", 7, 24), ("mtN", 8, 24), ("nM", 9, 999)])
        if args.show_change and args.mediatype:
            cols.append(("mtToSet", 10, 24))
    if args.show_change and args.description:
        cols.append(("descToSet", 26, max_desc_len + 3))
    if args.show_change and args.bandwidth:
        cols.append(("speedToSet", 27, 14))
    if args.show_change and args.duplex:
        cols.append(("dupToSet", 28, 12))
    if args.show_change and args.mtu:
        cols.append(("mtuToSet", 29, 8))
    if args.show_change and args.tx_power:
        cols.append(("txpToSet", 30, 10))
    if args.forwarding_model:
        cols.extend([("fwdF", 31, 14), ("fwdN", 32, 14), ("nFwd", 33, 999)])
    if args.show_change and args.forwarding_model:
        cols.append(("fwdToSet", 34, 14))
    if args.bandwidth:
        cols.extend([("bwF", 11, 14), ("speedN", 12, 14), ("nB", 13, 999)])
    if args.duplex:
        cols.extend([("dupF", 14, 12), ("dupN", 15, 12), ("nDup", 16, 999)])
    if args.mac:
        cols.extend([("macF", 17, 18), ("macN", 18, 18), ("nMac", 19, 999)])
    if args.mtu:
        cols.extend([("mtuF", 20, 8), ("mtuN", 21, 8), ("nMtu", 22, 999)])
    if args.tx_power:
        cols.extend([("txpF", 23, 10), ("txpN", 24, 10), ("nTxp", 25, 999)])
    return cols


def _row_to_dict(r, col_spec):
    """Собрать из строки row словарь только по ключам из col_spec (header -> value)."""
    d = {}
    for header, idx, max_w in col_spec:
        if idx < len(r):
            d[header] = r[idx]
    return d


def _print_combined_table(rows, note_codes_used, col_spec, max_desc_len=50):
    """Таблица только по колонкам из col_spec. col_spec: список (header, row_index, max_width)."""
    if not rows or not col_spec:
        return
    headers = [c[0] for c in col_spec]
    col_count = len(headers)
    widths = [len(h) for h in headers]
    for r in rows:
        for i, (_, idx, max_w) in enumerate(col_spec):
            if idx >= len(r):
                continue
            val = r[idx]
            if idx in (4, 5, 26) and val:
                val = (val or "")[:max_desc_len] + ("..." if len(val or "") > max_desc_len else "")
            elif idx in (7, 8, 10) and val:
                val = (val or "")[:20] + ("..." if len(val or "") > 20 else "")
            s = str(val or "")
            if len(s) > widths[i]:
                widths[i] = min(len(s), max_w)
    pad = "  "
    print(pad.join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print(pad.join("-" * widths[i] for i in range(col_count)))
    for r in rows:
        parts = []
        for i, (_, idx, max_w) in enumerate(col_spec):
            if idx >= len(r):
                parts.append("")
                continue
            val = r[idx]
            if idx in (4, 5, 26) and val:
                val = (val or "")[:max_desc_len] + ("..." if len(val or "") > max_desc_len else "")
            elif idx in (7, 8, 10) and val:
                val = (val or "")[:20] + ("..." if len(val or "") > 20 else "")
            parts.append(str(val or "").ljust(widths[i]))
        print(pad.join(parts))
    if note_codes_used:
        print()
        for code in sorted(note_codes_used):
            print("  {} — {}".format(code, ALL_LEGEND[code]))


if __name__ == "__main__":
    raise SystemExit(main())
