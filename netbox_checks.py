#!/usr/bin/env python3
"""
Проверки Netbox по данным из dry-ssh.json.
Достаём устройства из Netbox по тегу; если передан файл — сверяем hostname'ы
(различия выводим, совпадения не пишем). Ключ --intname: проверка имён интерфейсов (файл vs Netbox) с поиском по вариантам.
Ключ --description: сверка поля description (файл vs Netbox).
Ключ --mediatype: сверка mediaType / type (файл/SSH vs Netbox).
Ключ --mt-ref [FILE]: сверка mediaType со справочником (файл с interface_types);
  данные из файла/SSH и из Netbox сверяются со списком value в справочнике (по умолчанию netbox_interface_types.json).
Ключ --show-change: показывать колонки «что подставим в Netbox» по выбранным ключам (mediatype → mtToSet, description → descToSet, …). Если проверки не заданы — включаются все (--all); при явном --show-change все колонки выводятся, без скрытия групп без расхождений.
Ключ --apply: вносить изменения в Netbox при разнице по выбранным ключам (--mediatype, --description, --bandwidth, --duplex, --mtu, --tx-power).
  Справочник типов (--mt-ref) по умолчанию включён (netbox_interface_types.json); для типа в Netbox подставляется value/slug из справочника.

Переменные: NETBOX_URL, NETBOX_TOKEN, NETBOX_TAG.

По умолчанию таблица; с --json — JSON. В note — код замечания, расшифровка под таблицей.
При неверном/просроченном NETBOX_TOKEN — сообщение в stderr и выход с кодом 1.
"""

__version__ = "1.0"

import argparse
import json
import os
import re
import sys

import pynetbox

from uplinks_stats import (
    get_device_platform_name,
    is_arista_platform,
    is_juniper_platform,
    netbox_error_message,
)


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

# Код для IP-адресов (17)
IP_NOTE_DIFF = 17      # ipv4/ipv6 адреса (файл) и привязанные к интерфейсу в Netbox различаются

# Код для LAG / Related Interfaces (18)
LAG_NOTE_DIFF = 18     # aggregateInterface (файл) и lag (Netbox) различаются

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
    IP_NOTE_DIFF: "IPv4/IPv6 адреса (файл) и привязанные к интерфейсу в Netbox различаются",
    LAG_NOTE_DIFF: "aggregateInterface (файл) и LAG / Related Interfaces (Netbox) различаются",
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


def _normalize_ip_address(addr):
    """Нормализовать адрес для сравнения: строка без пробелов, нижний регистр для IPv6."""
    if addr is None:
        return ""
    s = str(addr).strip().lower()
    return s


def _is_global_routable_address(addr_with_prefix):
    """
    Только глобальные маршрутизируемые адреса (IPv4 и IPv6).
    Исключаем: private, link-local, unique local, loopback — как в uplinks_stats.
    """
    if not addr_with_prefix or not isinstance(addr_with_prefix, str):
        return False
    s = addr_with_prefix.strip().split("/")[0].lower()
    if ":" in s:
        if s == "::1" or s == "0:0:0:0:0:0:0:1":
            return False
        if s.startswith(("fe8", "fe9", "fea", "feb")):
            return False
        if s.startswith(("fc", "fd")):
            return False
        return True
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


def _get_interface_ip_addresses(nb, nb_iface):
    """
    Список IP (addr, vrf_id), привязанных к интерфейсу в Netbox. Только глобальные маршрутизируемые.
    vrf_id — id VRF в NetBox или None (global).
    """
    if nb is None or nb_iface is None:
        return []
    try:
        ip_list = list(nb.ipam.ip_addresses.filter(interface_id=nb_iface.id))
    except Exception:
        return []
    out = []
    for ip_obj in ip_list:
        addr = getattr(ip_obj, "address", None)
        if not addr:
            continue
        addr_norm = _normalize_ip_address(addr)
        if not _is_global_routable_address(addr_norm):
            continue
        vrf = getattr(ip_obj, "vrf", None)
        vrf_id = None
        if vrf is not None:
            vrf_id = vrf if isinstance(vrf, (int, type(None))) else getattr(vrf, "id", None)
        out.append((addr_norm, vrf_id))
    return sorted(out)


def _resolve_vrf_name_to_id(nb, vrf_name, cache):
    """По имени VRF в NetBox вернуть id. cache — dict для кэша (name -> id)."""
    if not nb or not vrf_name or not str(vrf_name).strip():
        return None
    name = str(vrf_name).strip()
    if name in cache:
        return cache[name]
    try:
        vrfs = list(nb.ipam.vrfs.filter(name=name))
        if vrfs:
            cache[name] = vrfs[0].id
            return vrfs[0].id
    except Exception:
        pass
    cache[name] = None
    return None


def _resolve_vrf_id_to_name(nb, vrf_id, id_to_name_cache):
    """По id VRF в NetBox вернуть имя для отображения. id_to_name_cache — dict (id -> name). None → '—'."""
    if vrf_id is None:
        return "—"
    if id_to_name_cache is not None and vrf_id in id_to_name_cache:
        return id_to_name_cache[vrf_id]
    name = "—"
    try:
        if nb:
            vrf_obj = nb.ipam.vrfs.get(vrf_id)
            if vrf_obj and getattr(vrf_obj, "name", None):
                name = vrf_obj.name
    except Exception:
        pass
    if id_to_name_cache is not None:
        id_to_name_cache[vrf_id] = name
    return name


def _find_ip_in_netbox(nb, address, vrf_id):
    """Найти в NetBox IP по address и vrf_id (None = global). Возврат список из 0 или 1 элемента."""
    try:
        if vrf_id is not None:
            candidates = list(nb.ipam.ip_addresses.filter(address=address, vrf_id=vrf_id))
        else:
            candidates = list(nb.ipam.ip_addresses.filter(address=address))
            candidates = [c for c in candidates if getattr(c, "vrf", None) is None]
        return candidates[:1]
    except Exception:
        return []


def _find_ip_in_netbox_any_vrf(nb, address):
    """Найти в NetBox любой IP с данным address (любой VRF или без VRF). Возврат список из 0 или 1 элемента."""
    try:
        candidates = list(nb.ipam.ip_addresses.filter(address=address))
        return candidates[:1]
    except Exception:
        return []


def _apply_ip_addresses_to_interface(nb, dev_name, iface_display_name, nb_iface, addrs_f, vrf_id_f=None):
    """
    Привести привязку IP к интерфейсу в NetBox к списку из файла.
    addrs_f — список глобальных адресов (строки), vrf_id_f — id VRF в NetBox или None (global).
    Учитывается VRF: один и тот же адрес в разных VRF считаются разными.
    """
    if nb is None or nb_iface is None:
        return
    addrs_n_tuples = _get_interface_ip_addresses(nb, nb_iface)
    set_f = set((a, vrf_id_f) for a in (addrs_f or []))
    set_n = set(addrs_n_tuples)
    to_remove = set_n - set_f
    to_add = set_f - set_n
    try:
        for addr, vrf_id_n in to_remove:
            existing = _find_ip_in_netbox(nb, addr, vrf_id_n)
            if existing:
                ip_obj = existing[0]
                ip_obj.assigned_object_id = None
                ip_obj.assigned_object_type = None
                ip_obj.save()
                print("IP {} {} {}: отвязан от интерфейса".format(dev_name, iface_display_name, addr), flush=True)
        for addr, vrf_id in to_add:
            existing = _find_ip_in_netbox(nb, addr, vrf_id)
            if existing:
                ip_obj = existing[0]
                cur_id = getattr(ip_obj, "assigned_object_id", None)
                if cur_id == nb_iface.id:
                    continue
                ip_obj.assigned_object_id = nb_iface.id
                ip_obj.assigned_object_type = "dcim.interface"
                ip_obj.save()
                print("IP {} {} {}: привязан к интерфейсу".format(dev_name, iface_display_name, addr), flush=True)
            else:
                # IP с нужным VRF не найден — возможно, адрес есть в другом VRF (например global); обновляем VRF и привязываем
                existing_any = _find_ip_in_netbox_any_vrf(nb, addr)
                if existing_any:
                    ip_obj = existing_any[0]
                    cur_vrf = getattr(ip_obj, "vrf", None)
                    cur_vrf_id = cur_vrf if isinstance(cur_vrf, (int, type(None))) else getattr(cur_vrf, "id", None)
                    cur_id = getattr(ip_obj, "assigned_object_id", None)
                    if cur_vrf_id != vrf_id or cur_id != nb_iface.id:
                        if cur_vrf_id != vrf_id:
                            ip_obj.vrf = vrf_id
                        if cur_id != nb_iface.id:
                            ip_obj.assigned_object_id = nb_iface.id
                            ip_obj.assigned_object_type = "dcim.interface"
                        ip_obj.save()
                        if cur_vrf_id != vrf_id:
                            print("IP {} {} {}: VRF изменён на целевой".format(dev_name, iface_display_name, addr), flush=True)
                        if cur_id != nb_iface.id:
                            print("IP {} {} {}: привязан к интерфейсу".format(dev_name, iface_display_name, addr), flush=True)
                else:
                    create_kw = dict(
                        address=addr,
                        assigned_object_id=nb_iface.id,
                        assigned_object_type="dcim.interface",
                    )
                    if vrf_id is not None:
                        create_kw["vrf"] = vrf_id
                    nb.ipam.ip_addresses.create(**create_kw)
                    print("IP {} {} {}: создан и привязан к интерфейсу".format(dev_name, iface_display_name, addr), flush=True)
    except Exception as e:
        print("Ошибка применения IP {} {}: {}".format(dev_name, iface_display_name, e), file=sys.stderr, flush=True)


def _apply_mac_to_interface(nb, dev_name, iface_display_name, nb_iface, mac_f):
    """
    Создать или найти запись MAC в NetBox, привязать к интерфейсу, выставить primary_mac_address.
    Если MAC уже есть, но привязан к другому интерфейсу — переносим на текущий (assigned_object_id).
    mac_f — значение physicalAddress из файла.
    """
    if not mac_f or not nb_iface:
        return
    mac_netbox = _mac_netbox_format(mac_f)
    if not mac_netbox:
        return
    try:
        existing = list(nb.dcim.mac_addresses.filter(mac_address=mac_netbox))
        if existing:
            rec = existing[0]
            url = getattr(rec, "url", None) or getattr(rec, "display", rec)
            mac_id = getattr(rec, "id", None)
            current_assigned_id = getattr(rec, "assigned_object_id", None)
            if current_assigned_id is not None and current_assigned_id != nb_iface.id:
                try:
                    # NetBox не даёт переназначить MAC, пока он primary на старом интерфейсе — сначала сбрасываем
                    old_iface = nb.dcim.interfaces.get(current_assigned_id)
                    primary = getattr(old_iface, "primary_mac_address", None) if old_iface else None
                    primary_id = primary if isinstance(primary, (int, type(None))) else getattr(primary, "id", None)
                    if old_iface is not None and primary_id == mac_id:
                        old_iface.update({"primary_mac_address": None})
                        print("MAC {} {} {}: сброшен primary на старом интерфейсе (id={})".format(dev_name, iface_display_name, mac_netbox, current_assigned_id), flush=True)
                    setattr(rec, "assigned_object_type", "dcim.interface")
                    setattr(rec, "assigned_object_id", nb_iface.id)
                    rec.save()
                    print("MAC {} {} {}: перенесён на интерфейс {} — {}".format(dev_name, iface_display_name, mac_netbox, iface_display_name, url), flush=True)
                except Exception as e_move:
                    print("Ошибка переноса MAC {} {} {} на интерфейс: {} — {}".format(dev_name, iface_display_name, mac_netbox, url, e_move), file=sys.stderr, flush=True)
                    return
            elif current_assigned_id is None:
                try:
                    setattr(rec, "assigned_object_type", "dcim.interface")
                    setattr(rec, "assigned_object_id", nb_iface.id)
                    rec.save()
                    print("MAC {} {} {}: привязан к интерфейсу — {}".format(dev_name, iface_display_name, mac_netbox, url), flush=True)
                except Exception as e_bind:
                    print("Ошибка привязки MAC {} {} {}: {} — {}".format(dev_name, iface_display_name, mac_netbox, url, e_bind), file=sys.stderr, flush=True)
                    return
            else:
                print("MAC {} {} {}: уже в Netbox на этом интерфейсе — {}".format(dev_name, iface_display_name, mac_netbox, url), flush=True)
        else:
            created = nb.dcim.mac_addresses.create(
                mac_address=mac_netbox,
                assigned_object_type="dcim.interface",
                assigned_object_id=nb_iface.id,
            )
            rec = created
            url = getattr(created, "url", None) or getattr(created, "display", created)
            print("MAC {} {} {}: создан — {}".format(dev_name, iface_display_name, mac_netbox, url), flush=True)
        mac_id = getattr(rec, "id", None)
        if mac_id is not None:
            try:
                nb_iface.update({"primary_mac_address": mac_id})
                print("Обновлено {} {}: primary_mac_address={}".format(dev_name, iface_display_name, mac_id), flush=True)
            except Exception as e2:
                print("Ошибка установки primary_mac_address {} {}: {} — {}".format(dev_name, iface_display_name, mac_id, e2), file=sys.stderr, flush=True)
    except Exception as e:
        print("Ошибка MAC {} {} {}: {} — {}".format(dev_name, iface_display_name, mac_netbox, e), file=sys.stderr, flush=True)


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
        prog="netbox_checks.py",
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
        default="all",
        help="Платформа в NetBox: arista, juniper или all (по умолчанию — все)",
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
        "--ip-address",
        action="store_true",
        dest="ip_address",
        help="IPv4/IPv6 адреса (файл: ipv4_addresses, ipv6_addresses) vs привязанные к интерфейсу в Netbox",
    )
    g_checks.add_argument(
        "--lag",
        action="store_true",
        help="LAG / Related Interfaces: aggregateInterface (файл) vs lag (Netbox) у физических интерфейсов",
    )
    g_checks.add_argument(
        "--all",
        action="store_true",
        dest="all_checks",
        help="Включить все проверки сразу",
    )
    # --- Вывод ---
    g_out = parser.add_argument_group("Вывод")
    g_out.add_argument("--version", action="version", version="%(prog)s " + __version__)
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
        "--hide-ok-hosts",
        action="store_true",
        dest="hide_ok_hosts",
        help="Не выводить в таблице хосты без расхождений; вывести их списком и статистику (хосты/интерфейсы OK и с расхождениями)",
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
        args.ip_address = True
        args.lag = True
    if args.no_mt_ref:
        args.mt_ref = None
    # При одном --hide-ok-hosts без проверок включаем все проверки (чтобы был вывод списка и статистики)
    if args.hide_ok_hosts and not (
        args.intname or args.description or args.mediatype or args.bandwidth or args.duplex
        or args.mac or args.mtu or args.tx_power or args.forwarding_model or args.ip_address or args.lag
    ):
        args.intname = True
        args.description = True
        args.mediatype = True
        args.bandwidth = True
        args.duplex = True
        args.mac = True
        args.mtu = True
        args.tx_power = True
        args.forwarding_model = True
        args.ip_address = True
        args.lag = True

    has_checks = (
        args.intname or args.description or args.mediatype or args.bandwidth or args.duplex
        or args.mac or args.mtu or args.tx_power or args.forwarding_model or args.ip_address or args.lag
    )
    show_change_requested = args.show_change
    if not has_checks and not args.apply:
        args.intname = True
        args.description = True
        args.mediatype = True
        args.bandwidth = True
        args.duplex = True
        args.mac = True
        args.mtu = True
        args.tx_power = True
        args.forwarding_model = True
        args.ip_address = True
        args.lag = True
        args.show_change = True
        # Не скрывать колонки без расхождений, если пользователь явно запросил --show-change
        args.hide_no_diff_cols = not show_change_requested

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
    try:
        nb = pynetbox.api(url, token=token)
        nb_devices = list(nb.dcim.devices.filter(tag=netbox_tag))
    except Exception as e:
        print("Ошибка доступа к NetBox: {}.".format(netbox_error_message(e)), file=sys.stderr)
        return 1
    nb_names = [d.name for d in nb_devices]
    nb_by_name = {d.name: d for d in nb_devices}

    # Сверка hostname'ов (при --host не выводим: список «только в Netbox» не нужен)
    only_file, only_nb = compare_hostnames(file_devices, nb_names)
    if args.platform != "all" and only_nb:
        only_nb = [
            n for n in only_nb
            if n in nb_by_name
            and (
                (args.platform == "arista" and is_arista_platform(get_device_platform_name(nb_by_name[n], nb)))
                or (args.platform == "juniper" and is_juniper_platform(get_device_platform_name(nb_by_name[n], nb)))
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
    # Справочник типов нужен при сверке mediatype и при создании интерфейсов (--apply --intname), чтобы подставить slug NetBox
    if (args.mediatype or (args.apply and args.intname)) and args.mt_ref:
        mt_ref_values, mt_ref_list, ref_err = load_mt_ref(args.mt_ref)
        if ref_err:
            print("Справочник типов (--mt-ref): {}.".format(ref_err), flush=True)
        elif mt_ref_values:
            print("Справочник типов загружен: {} записей ({}).".format(len(mt_ref_values), args.mt_ref), flush=True)

    if args.mediatype and mt_ref_values is None:
        print("Внимание: без справочника типов (--mt-ref) значения mediaType не приводятся к одному формату, "
              "сверка может показывать расхождения из-за разного написания (файл vs Netbox).", file=sys.stderr, flush=True)

    out = {}
    if args.intname or args.description or args.mediatype or (args.show_change and mt_ref_values) or args.bandwidth or args.duplex or args.mac or args.mtu or args.tx_power or args.forwarding_model or args.ip_address or args.lag:
        # Один проход: строки (..., mtToSet=10, ...); индексы 26-30 — *ToSet при --show-change
        rows = []
        note_codes_used = set()
        vrf_cache = {}
        vrf_id_to_name_cache = {}
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
                platform_name = get_device_platform_name(device, nb)
                if args.platform == "arista" and not is_arista_platform(platform_name):
                    skipped_platform.append(dev_name)
                    continue
                if args.platform == "juniper" and not is_juniper_platform(platform_name):
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
                is_logical_unit = entry.get("isLogical") or (int_name and "." in str(int_name) and str(int_name).startswith("ae"))
                mt_f = (entry.get("mediaType") or "").strip() if (args.mediatype or args.show_change) else ""
                mt_n = ""
                nM = ""
                mt_to_set = ""
                if args.mediatype or args.show_change:
                    if nb_iface is not None:
                        mt_n = _netbox_type_to_str(nb_iface)
                    # LAG: в файле mediaType не задаётся, в NetBox type=lag — не считать расхождением
                    is_lag = entry.get("isLag") or (int_name and str(int_name).startswith("ae") and "." not in str(int_name))
                    is_lag_ok = is_lag and not mt_f and mt_n == "lag"
                    # Logical unit (ae5.0): в файле mediaType не задаётся, в NetBox type=virtual — не считать расхождением
                    is_logical_ok = is_logical_unit and not mt_f and mt_n == "virtual"
                    type_ok = is_lag_ok or is_logical_ok
                    ref_list = mt_ref_list or []
                    mt_f_value = _mt_to_value(mt_f, mt_ref_values or set(), ref_list) if mt_ref_values else mt_f
                    mt_n_value = _mt_to_value(mt_n, mt_ref_values or set(), ref_list) if mt_ref_values else mt_n
                    # slug для Netbox (value из справочника) — для вывода mtToSet и для --apply
                    if mt_ref_values and mt_f_value:
                        mt_to_set = mt_f_value
                    if args.mediatype and nb_iface is not None:
                        if mt_ref_values:
                            if mt_f_value and mt_n_value and mt_f_value != mt_n_value and not type_ok:
                                nM = str(MT_NOTE_DIFF)
                                note_codes_used.add(MT_NOTE_DIFF)
                        else:
                            if mt_f != mt_n and not type_ok:
                                nM = str(MT_NOTE_DIFF)
                                note_codes_used.add(MT_NOTE_DIFF)
                    if mt_ref_values is not None:
                        n_codes = []
                        if mt_f and not _mt_in_ref(mt_f, mt_ref_values, ref_list):
                            n_codes.append(str(MT_NOTE_F_NOT_IN_REF))
                            note_codes_used.add(MT_NOTE_F_NOT_IN_REF)
                        if mt_n and mt_n not in mt_ref_values and not type_ok:
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
                    if not dup_f_norm and entry.get("bandwidth") and entry.get("bandwidth") >= 10_000_000_000:
                        dup_f_norm = "full"  # 10G+ только full duplex, проверять не имеет смысла
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
                    # MAC проверяем только у физических интерфейсов (не LAG, не logical unit)
                    is_lag_for_mac = entry.get("isLag") or (int_name and str(int_name).startswith("ae") and "." not in str(int_name))
                    is_logical = entry.get("isLogical") or (int_name and "." in str(int_name))
                    is_physical_for_mac = not is_lag_for_mac and not is_logical
                    if not is_physical_for_mac:
                        mac_f = ""
                        mac_n = ""
                    if is_physical_for_mac:
                        mac_f_norm = _normalize_mac(mac_f)
                        mac_n_norm = _normalize_mac(mac_n)
                        if mac_f_norm and (not mac_n_norm or mac_f_norm != mac_n_norm):
                            nMac = str(MAC_NOTE_DIFF)
                            note_codes_used.add(MAC_NOTE_DIFF)
                        if nb_iface and mac_n_norm and not _mac_both_filled(nb_iface):
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
                    # У logical unit в файле forwardingModel не задаётся — не считать расхождением
                    fwd_f_cmp = (_fwd_file_to_netbox_mode(fwd_f) or "")
                    fwd_n_cmp = (fwd_n or "").strip().lower()
                    if fwd_f_cmp != fwd_n_cmp and not (is_logical_unit and not fwd_f):
                        nFwd = str(FWD_NOTE_DIFF)
                        note_codes_used.add(FWD_NOTE_DIFF)
                fwd_to_set = (_fwd_file_to_netbox_mode(fwd_f) or "") if (args.show_change and args.forwarding_model and fwd_f and nFwd) else ""
                ip_f = ""
                ip_n = ""
                ip_vrf_f_display = ""
                ip_vrf_n_display = ""
                nIp = ""
                if args.ip_address:
                    ipv4_f = entry.get("ipv4_addresses") or []
                    ipv6_f = entry.get("ipv6_addresses") or []
                    if not isinstance(ipv4_f, list):
                        ipv4_f = [ipv4_f] if ipv4_f is not None else []
                    if not isinstance(ipv6_f, list):
                        ipv6_f = [ipv6_f] if ipv6_f is not None else []
                    addrs_f = sorted([n for a in ipv4_f + ipv6_f if a for n in (_normalize_ip_address(a),) if _is_global_routable_address(n)])
                    ip_vrf_f = (entry.get("ip_vrf") or "").strip() or None
                    vrf_id_f = _resolve_vrf_name_to_id(nb, ip_vrf_f, vrf_cache) if ip_vrf_f else None
                    addrs_n_tuples = _get_interface_ip_addresses(nb, nb_iface) if nb_iface else []
                    set_f = set((a, vrf_id_f) for a in (addrs_f or []))
                    set_n = set(addrs_n_tuples)
                    ip_f = ", ".join(addrs_f) if addrs_f else ""
                    ip_n = ", ".join(a for a, _ in addrs_n_tuples) if addrs_n_tuples else ""
                    ip_vrf_f_display = (ip_vrf_f or "—").strip() if ip_vrf_f else "—"
                    vrf_ids_n = list({vid for _, vid in addrs_n_tuples})
                    if not vrf_ids_n:
                        ip_vrf_n_display = "—"
                    else:
                        names_n = [_resolve_vrf_id_to_name(nb, vid, vrf_id_to_name_cache) for vid in vrf_ids_n]
                        ip_vrf_n_display = ", ".join(sorted(set(names_n)))
                    if set_f != set_n:
                        nIp = str(IP_NOTE_DIFF)
                        note_codes_used.add(IP_NOTE_DIFF)
                lag_f = ""
                lag_n = ""
                nLag = ""
                if args.lag:
                    aggregate_name_f = (entry.get("aggregateInterface") or "").strip()
                    is_physical_for_lag = not entry.get("isLag") and not is_logical_unit
                    if is_physical_for_lag:
                        lag_f = aggregate_name_f or ""
                        if nb_iface is not None:
                            lag_obj = getattr(nb_iface, "lag", None)
                            if lag_obj is not None:
                                lag_n = getattr(lag_obj, "name", None) or str(lag_obj)
                            lag_n = (lag_n or "").strip()
                        if lag_f != lag_n:
                            nLag = str(LAG_NOTE_DIFF)
                            note_codes_used.add(LAG_NOTE_DIFF)
                # значения «что подставим» для --show-change — только когда есть расхождение (есть что применять)
                desc_to_set = desc_f if (args.show_change and args.description and nD) else ""
                speed_to_set = (bw_f // 1000) if (args.show_change and args.bandwidth and bw_f is not None and nB) else ""
                dup_to_set = (_normalize_duplex(dup_f) or dup_f) if (args.show_change and args.duplex and dup_f and nDup) else ""
                mtu_to_set_val = mtu_f if (args.show_change and args.mtu and mtu_f != "") else ""
                mtu_to_set_display = mtu_to_set_val if nMtu else ""
                txp_to_set = txp_f_out if (args.show_change and args.tx_power and nTxp) else ""
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
                    if args.duplex and nDup and dup_f:
                        dup_val = _normalize_duplex(dup_f) or dup_f
                        if dup_val:
                            updates["duplex"] = dup_val
                    if args.mtu and mtu_f != "" and (mtu_n == "" or mtu_f != mtu_n):
                        updates["mtu"] = int(mtu_f) if isinstance(mtu_f, (int, float)) else mtu_f
                    if args.tx_power and txp_f != "" and isinstance(txp_f, (int, float)) and (txp_n == "" or txp_f != txp_n):
                        updates["tx_power"] = txp_f
                    if args.forwarding_model and nFwd and fwd_f:
                        # В Netbox: routed → mode=null, bridged → mode=tagged
                        updates["mode"] = _fwd_file_to_netbox_mode(fwd_f)
                    # Related Interfaces (LAG): физические члены LAG должны ссылаться на интерфейс LAG
                    aggregate_name = (entry.get("aggregateInterface") or "").strip()
                    if aggregate_name and not entry.get("isLag") and not is_logical_unit:
                        nb_lag = nb_by_iface_name.get(aggregate_name)
                        if nb_lag:
                            cur_lag = getattr(nb_iface, "lag", None)
                            cur_lag_id = cur_lag if isinstance(cur_lag, (int, type(None))) else getattr(cur_lag, "id", None)
                            if cur_lag_id != getattr(nb_lag, "id", None):
                                updates["lag"] = nb_lag.id
                    if updates:
                        try:
                            nb_iface.update(updates)
                            print("Обновлено {} {}: {}".format(dev_name, nb_name or int_name, list(updates.keys())), flush=True)
                        except Exception as e:
                            print("Ошибка обновления {} {}: {} — {}".format(dev_name, nb_name or int_name, updates, e), file=sys.stderr, flush=True)
                    # MAC в Netbox — отдельная сущность (dcim.mac-addresses); только у физических интерфейсов
                    if args.mac and is_physical_for_mac and (nMac or (mac_f and not mac_n)) and nb_iface is not None:
                        _apply_mac_to_interface(nb, dev_name, nb_name or int_name, nb_iface, mac_f)
                    # IP в Netbox — ipam.ip_addresses с assigned_object_id/type; привести к списку из файла
                    if args.ip_address and nIp and nb_iface is not None:
                        _apply_ip_addresses_to_interface(nb, dev_name, nb_name or int_name, nb_iface, addrs_f, vrf_id_f)
                elif args.apply and args.intname and note_code == NOTE_MISSING:
                    # Интерфейс в NetBox не найден — создаём и сразу заполняем все поля из файла
                    create_data = {"device": device.id, "name": int_name}
                    # LAG (агрегат ae): в NetBox тип Link Aggregation Group (LAG), slug = "lag"
                    if entry.get("isLag"):
                        create_data["type"] = "lag"
                    elif is_logical_unit:
                        create_data["type"] = "virtual"
                    else:
                        media_from_file = (entry.get("mediaType") or "").strip()
                        mt_raw = mt_to_set or media_from_file
                        if mt_ref_values and mt_ref_list and media_from_file:
                            mt_resolved = _mt_to_value(media_from_file, mt_ref_values, mt_ref_list)
                            if mt_resolved and mt_resolved in mt_ref_values:
                                mt_raw = mt_resolved
                        if mt_raw:
                            create_data["type"] = mt_raw
                    desc_raw = (entry.get("description") or "").strip()
                    if desc_raw:
                        create_data["description"] = desc_raw
                    bw_raw = entry.get("bandwidth")
                    if bw_raw is not None:
                        create_data["speed"] = int(bw_raw) // 1000  # bps -> Kbps
                    dup_raw = (entry.get("duplex") or "").strip()
                    dup_val = _normalize_duplex(dup_raw) or (dup_raw if dup_raw else None)
                    if not dup_val and bw_raw is not None and int(bw_raw) >= 10_000_000_000:
                        dup_val = "full"
                    if dup_val:
                        create_data["duplex"] = dup_val
                    mtu_raw = entry.get("mtu")
                    if mtu_raw is not None and mtu_raw != "":
                        try:
                            create_data["mtu"] = int(mtu_raw)
                        except (TypeError, ValueError):
                            pass
                    txp_raw = entry.get("txPower")
                    if txp_raw is not None:
                        try:
                            create_data["tx_power"] = int(round(float(txp_raw)))
                        except (TypeError, ValueError):
                            pass
                    fwd_raw = (entry.get("forwardingModel") or "").strip()
                    if fwd_raw:
                        mode_val = _fwd_file_to_netbox_mode(fwd_raw)
                        if mode_val is not None:
                            create_data["mode"] = mode_val
                    try:
                        nb_iface = nb.dcim.interfaces.create(**create_data)
                        nb_by_iface_name[int_name] = nb_iface  # чтобы второй проход (LAG) и последующие записи видели новый интерфейс
                        print("Создан интерфейс {} {}: {}".format(dev_name, int_name, list(create_data.keys())), flush=True)
                        if args.mac and is_physical_for_mac and mac_f:
                            _apply_mac_to_interface(nb, dev_name, int_name, nb_iface, mac_f)
                        if args.ip_address and addrs_f:
                            _apply_ip_addresses_to_interface(nb, dev_name, int_name, nb_iface, addrs_f, vrf_id_f)
                    except Exception as e:
                        print("Ошибка создания {} {}: {} — {}".format(dev_name, int_name, create_data, e), file=sys.stderr, flush=True)
                mt_to_set_display = mt_to_set if nM else ""
                rows.append((dev_name, int_name, nb_name, note, desc_f, desc_n, nD, mt_f, mt_n, nM, mt_to_set_display, bw_f, speed_n, nB, dup_f_out, dup_n_out, nDup, mac_f, mac_n, nMac, mtu_f, mtu_n, nMtu, txp_f_out, txp_n_out, nTxp, desc_to_set, speed_to_set, dup_to_set, mtu_to_set_display, txp_to_set, fwd_f, fwd_n, nFwd, fwd_to_set, ip_f, ip_n, ip_vrf_f_display, ip_vrf_n_display, nIp, lag_f, lag_n, nLag))
            # Второй проход: выставить LAG (Related Interfaces) у физических интерфейсов — при создании физического LAG мог ещё не существовать
            if args.apply and args.intname:
                for entry in payload:
                    if not isinstance(entry, dict):
                        continue
                    int_name = entry.get("name")
                    aggregate_name = (entry.get("aggregateInterface") or "").strip()
                    if not int_name or not aggregate_name or entry.get("isLag"):
                        continue
                    if entry.get("isLogical") or (int_name and "." in str(int_name) and str(int_name).startswith("ae")):
                        continue
                    _, nb_phys = resolve_interface(int_name, nb_by_iface_name)
                    nb_lag = nb_by_iface_name.get(aggregate_name) if aggregate_name else None
                    if not nb_phys or not nb_lag:
                        continue
                    cur_lag = getattr(nb_phys, "lag", None)
                    cur_lag_id = cur_lag if isinstance(cur_lag, (int, type(None))) else getattr(cur_lag, "id", None)
                    if cur_lag_id != getattr(nb_lag, "id", None):
                        try:
                            nb_phys.update({"lag": nb_lag.id})
                            print("{} {}: привязан к LAG {}".format(dev_name, int_name, aggregate_name), flush=True)
                        except Exception as e:
                            print("Ошибка привязки {} {} к LAG {}: {}".format(dev_name, int_name, aggregate_name, e), file=sys.stderr, flush=True)
        if skipped_no_netbox:
            print("Пропущено (устройство есть в файле, но нет в Netbox по тегу): {}.".format(", ".join(skipped_no_netbox)))
        if skipped_not_list:
            print("Пропущено (в файле не список интерфейсов): {}.".format(", ".join(skipped_not_list)))
        if skipped_platform:
            print("Пропущено (платформа не {}): {}.".format(args.platform, ", ".join(skipped_platform)))
        # Статистика и фильтр «хосты без расхождений» при --hide-ok-hosts
        rows_display = rows
        ok_hosts = set()
        not_ok_hosts_set = set()
        hosts_ok_count = 0
        hosts_not_ok_count = 0
        interfaces_ok_count = 0
        interfaces_not_ok_count = 0
        if rows:
            all_hosts = set(r[0] for r in rows)
            ok_hosts = {h for h in all_hosts if all(not _row_has_diff(r) for r in rows if r[0] == h)}
            not_ok_hosts_set = all_hosts - ok_hosts
            hosts_ok_count = len(ok_hosts)
            hosts_not_ok_count = len(not_ok_hosts_set)
            interfaces_ok_count = sum(1 for r in rows if not _row_has_diff(r))
            interfaces_not_ok_count = len(rows) - interfaces_ok_count
            if args.hide_ok_hosts:
                rows_display = [r for r in rows if r[0] not in ok_hosts]
                ok_list = sorted(ok_hosts)
                if ok_list:
                    print("Хосты без расхождений ({}): {}".format(len(ok_list), ", ".join(ok_list)), flush=True)
                print("Статистика: хосты OK {}, хосты с расхождениями {}; интерфейсы OK {}, интерфейсы с расхождениями {}.".format(
                    hosts_ok_count, hosts_not_ok_count, interfaces_ok_count, interfaces_not_ok_count), flush=True)
                if rows_display or not args.apply:
                    print(flush=True)
        if not args.apply:
            col_spec = _build_col_spec(args)
            if args.hide_empty_note_cols and rows_display:
                col_spec = _filter_empty_note_cols(col_spec, rows_display)
            if args.hide_no_diff_cols and rows_display:
                col_spec = _filter_no_diff_cols(col_spec, rows_display)
            has_any_diff = any(_row_has_diff(r) for r in rows_display) if rows_display else False
            if rows_display and not has_any_diff:
                print("Итог: все проверенные поля совпадают с NetBox. Расхождений не найдено.", flush=True)
                print(flush=True)
            if args.json:
                out["rows"] = [
                    _row_to_dict(r, col_spec)
                    for r in rows_display
                ]
                out["note_legend"] = {str(k): v for k, v in ALL_LEGEND.items() if k in note_codes_used}
                if args.hide_ok_hosts and rows:
                    out["ok_hosts"] = sorted(ok_hosts)
                    out["not_ok_hosts"] = sorted(not_ok_hosts_set)
                    out["stats"] = {
                        "hosts_ok": hosts_ok_count,
                        "hosts_not_ok": hosts_not_ok_count,
                        "interfaces_ok": interfaces_ok_count,
                        "interfaces_not_ok": interfaces_not_ok_count,
                    }
            else:
                _print_combined_table(rows_display, note_codes_used, col_spec)
        else:
            print("При --apply таблица не выводится (данные уже обновлены в Netbox).", flush=True)

    if args.json and out:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


# Индексы колонок примечаний в строке (note, nD, nM, nB, nDup, nMac, nMtu, nTxp, nFwd, nIp, nLag) — для определения «есть расхождение»
ROW_NOTE_INDICES = (3, 6, 9, 13, 16, 19, 22, 25, 33, 39, 42)


def _row_has_diff(row):
    """Есть ли в строке хотя бы одно примечание/расхождение."""
    for i in ROW_NOTE_INDICES:
        if i < len(row) and row[i]:
            return True
    return False


# Заголовки колонок-примечаний (nD, nM, nB, …) — для --hide-empty-note-cols
NOTE_COL_HEADERS = {"nD", "nM", "nB", "nDup", "nMac", "nMtu", "nTxp", "nFwd", "nIp", "nLag"}

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
    "nIp": {"ipF", "ipN", "ipVrfF", "ipVrfN", "nIp"},
    "nLag": {"lagF", "lagN", "nLag"},
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
    if args.ip_address:
        cols.extend([("ipF", 35, 60), ("ipN", 36, 60), ("ipVrfF", 37, 12), ("ipVrfN", 38, 12), ("nIp", 39, 999)])
    if args.lag:
        cols.extend([("lagF", 40, 12), ("lagN", 41, 12), ("nLag", 42, 999)])
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
