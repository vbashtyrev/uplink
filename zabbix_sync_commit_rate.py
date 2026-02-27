#!/usr/bin/env python3
"""
Синхронизация макросов commit rate в Zabbix из NetBox: для каждого интерфейса с circuit
(кабель от termination A к интерфейсу) создаются макросы с commit_rate в bps.

Макросы с контекстом по интерфейсу:
- **{$IF.UTIL.MAX:"Ethernet51/1"}** — 100% порог (линия на графике, красный линк на карте);
- **{$IF.UTIL.WARN:"Ethernet51/1"}** — 90% порог (жёлтый линк на карте).

Скрипт создаёт два простых триггера на интерфейс:
- max(Bits received, 5m) > {$IF.UTIL.WARN:"<интерфейс>"} — при 90% (Warning, жёлтый);
- max(Bits received, 5m) > {$IF.UTIL.MAX:"<интерфейс>"} — при 100% (High, красный).
Линия порога на дашборде рисуется Simple triggers по триггеру 100%. Карта (zabbix_map.py) привязывает
эти триггеры к линкам, чтобы цвет линка менялся при достижении порогов.

Удаляются старые item'ы net.if.threshold[...], если остались.

Для устройств, где в NetBox кабель на физическом интерфейсе (напр. et-0/0/3), а в Zabbix — логическом
(ae5.0, ae3.0), задайте -d dry-ssh.json: макрос будет по логическому имени.

Переменные: NETBOX_URL, NETBOX_TOKEN, NETBOX_TAG, ZABBIX_URL, ZABBIX_TOKEN.
"""

import json
import os
import sys

import pynetbox

# Общая логика Zabbix API из zabbix_map
from zabbix_map import (
    _get_zabbix_url_token,
    _interface_from_key,
    _interface_from_item_name,
    _normalize_interface_name,
    validate_zabbix_token,
    zabbix_request,
)

MACRO_PREFIX = "{$IF.UTIL.MAX"  # Для поиска старых макросов при удалении
MACRO_PREFIX_WARN = "{$IF.UTIL.WARN"  # Макрос 90% для предупреждения (жёлтый линк на карте)
# Префикс ключа старых item'ов порога (удаляются при синке)
THRESHOLD_ITEM_KEY = 'net.if.threshold'
# NetBox commit_rate в Kbps → в bps для Zabbix
KBPS_TO_BPS = 1000
DEFAULT_DRY_SSH = "dry-ssh.json"


def _macro_name_for_interface(iface_name):
    """Полное имя макроса с контекстом: {$IF.UTIL.MAX:"Ethernet51/1"} — для использования в триггере."""
    if not iface_name:
        iface_name = ""
    return '{$IF.UTIL.MAX:"' + iface_name.strip() + '"}'


def _macro_name_warn_for_interface(iface_name):
    """Макрос 90% порога: {$IF.UTIL.WARN:"Ethernet51/1"} — для триггера «жёлтый» на карте."""
    if not iface_name:
        iface_name = ""
    return '{$IF.UTIL.WARN:"' + iface_name.strip() + '"}'


def load_dry_ssh(path):
    """Загрузить dry-ssh.json. Возврат devices dict или None."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("devices") or None


def build_physical_to_logical(dry_ssh_devices):
    """
    По dry-ssh: для каждого (device, physical_interface) список логических интерфейсов,
    у которых physicalInterface == physical_interface.
    Возврат: dict (dev_name, physical_iface) -> [logical_name, ...]
    """
    out = {}
    if not dry_ssh_devices:
        return out
    for dev_name, ifaces in dry_ssh_devices.items():
        if not isinstance(ifaces, list):
            continue
        for entry in ifaces:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip()
            phys = (entry.get("physicalInterface") or "").strip()
            if not name or not phys:
                continue
            key = (dev_name, phys)
            out.setdefault(key, []).append(name)
    return out


def _pick_one_logical(logicals):
    """
    Из нескольких логических интерфейсов на одном физическом выбрать один для макроса Zabbix.
    Приоритет: unit .0 (ae5.0, ae3.0) — основной uplink LAG; иначе первый в списке.
    """
    if not logicals:
        return None
    if len(logicals) == 1:
        return logicals[0]
    for name in logicals:
        if name.endswith(".0"):
            return name
    return logicals[0]


def apply_logical_context(commit_rates, dry_ssh_devices, debug=False):
    """
    Если задан dry_ssh: для пар (dev, physical_iface) из NetBox подставить контекст по логическому
    имени (как в Zabbix). Один контур → один макрос на логический интерфейс (при нескольких
    логических на одной физике берётся один, приоритет — unit .0, напр. ae5.0).
    Возврат: dict (device_name, iface_name_for_zabbix) -> commit_rate_bps
    """
    phys_to_logical = build_physical_to_logical(dry_ssh_devices)
    result = {}
    substituted = []
    for (dev_name, iface_name), bps in commit_rates.items():
        key = (dev_name, iface_name)
        logicals = phys_to_logical.get(key, [])
        if logicals:
            logical = _pick_one_logical(logicals)
            if logical:
                result[(dev_name, logical)] = bps
                substituted.append((dev_name, iface_name, logical))
            else:
                result[(dev_name, iface_name)] = bps
        else:
            result[(dev_name, iface_name)] = bps
    if debug and substituted:
        for dev, phys, logical in substituted:
            print("Контекст для Zabbix: {} {} -> {}".format(dev, phys, logical), file=sys.stderr)
    return result


def get_commit_rates_from_netbox(nb, tag, debug=False):
    """
    По NetBox: интерфейсы, подключённые кабелем к circuit termination (A), и commit_rate контура.
    Возврат: dict (device_name, interface_name) -> commit_rate_bps (int).
    Учитываются только устройства с тегом tag (фильтр по тегу обязателен).
    """
    result = {}
    try:
        cts = list(nb.circuits.circuit_terminations.filter(term_side="A"))
    except Exception as e:
        if debug:
            print("circuit_terminations.filter: {}".format(e), file=sys.stderr)
        return result

    if debug:
        print("NetBox: circuit terminations (A): {}, с кабелем к dcim.interface, фильтр по тегу {!r}".format(len(cts), tag or "(нет)"), file=sys.stderr)

    device_ids_by_tag = set()
    if tag:
        try:
            devices_tagged = list(nb.dcim.devices.filter(tag=tag))
            device_ids_by_tag = {d.id for d in devices_tagged}
            if debug:
                print("Устройства с тегом {!r}: {} шт.".format(tag, len(device_ids_by_tag)), file=sys.stderr)
        except Exception as e:
            if debug:
                print("filter(tag=): {}".format(e), file=sys.stderr)

    skipped_no_cable = 0
    skipped_no_interface = 0
    skipped_tag = 0

    for ct in cts:
        cable = getattr(ct, "cable", None)
        if cable is None:
            skipped_no_cable += 1
            continue
        cable_id = cable.id if hasattr(cable, "id") else cable
        if not cable_id:
            skipped_no_cable += 1
            continue
        try:
            cable_obj = nb.dcim.cables.get(cable_id)
        except Exception:
            if debug:
                print("cables.get({}) failed".format(cable_id), file=sys.stderr)
            continue
        if not cable_obj:
            continue

        a_terms = getattr(cable_obj, "a_terminations", None) or []
        b_terms = getattr(cable_obj, "b_terminations", None) or []
        if not isinstance(a_terms, list):
            a_terms = [a_terms] if a_terms else []
        if not isinstance(b_terms, list):
            b_terms = [b_terms] if b_terms else []

        # Один конец — circuit termination, другой — interface
        interface_oid = None
        for term in a_terms + b_terms:
            if isinstance(term, dict):
                ot = term.get("object_type") or term.get("object_type_id")
                oid = term.get("object_id")
            else:
                ot = getattr(term, "object_type", None) or getattr(term, "object_type_id", None)
                oid = getattr(term, "object_id", None)
            if not oid:
                continue
            ot = (ot or "").lower()
            if "interface" in ot and "circuit" not in ot:
                interface_oid = oid
                break
        if not interface_oid:
            skipped_no_interface += 1
            continue

        try:
            iface = nb.dcim.interfaces.get(interface_oid)
        except Exception:
            continue
        if not iface:
            continue

        device = getattr(iface, "device", None)
        if device is None:
            try:
                dev_id = getattr(iface, "device_id", None) or iface.device
                if dev_id is not None:
                    device = nb.dcim.devices.get(dev_id)
            except Exception:
                pass
        if not device:
            continue
        dev_id = device.id if hasattr(device, "id") else device
        if tag and dev_id not in device_ids_by_tag:
            skipped_tag += 1
            continue
        device_name = getattr(device, "name", None) or ""
        iface_name = getattr(iface, "name", None) or ""
        if not device_name or not iface_name:
            continue

        circuit = getattr(ct, "circuit", None)
        if circuit is None:
            try:
                cid = getattr(ct, "circuit_id", None) or ct.circuit
                if cid is not None:
                    circuit = nb.circuits.circuits.get(cid)
            except Exception:
                pass
        if not circuit:
            continue
        commit_rate_kbps = getattr(circuit, "commit_rate", None)
        if commit_rate_kbps is None:
            continue
        try:
            commit_rate_kbps = int(commit_rate_kbps)
        except (TypeError, ValueError):
            continue
        commit_rate_bps = commit_rate_kbps * KBPS_TO_BPS
        result[(device_name, iface_name)] = commit_rate_bps

    if debug:
        print("Пропущено: без кабеля {}, не интерфейс {}, по тегу {}; итого пар: {}".format(
            skipped_no_cable, skipped_no_interface, skipped_tag, len(result)), file=sys.stderr)
    return result


def get_zabbix_host_macros(url, token, hostids, debug=False):
    """Получить макросы хостов. Возврат hostid -> list of {macro, value, type, context?, hostmacroid?}."""
    if not hostids:
        return {}
    result, err = zabbix_request(
        url, token, "usermacro.get",
        {"hostids": list(hostids), "output": ["macro", "value", "type", "context", "hostmacroid"]},
        debug=debug,
    )
    if err:
        return {str(hid): [] for hid in hostids}
    out = {str(hid): [] for hid in hostids}
    for m in (result or []):
        hid = str(m.get("hostid", ""))
        if not hid or hid not in out:
            continue
        entry = {"macro": m.get("macro", ""), "value": m.get("value", ""), "type": str(m.get("type", "0"))}
        if m.get("context") not in (None, ""):
            entry["context"] = m.get("context")
        if m.get("hostmacroid") is not None:
            entry["hostmacroid"] = m["hostmacroid"]
        out[hid].append(entry)
    return out


def set_zabbix_host_if_util_macros(url, token, hostid, new_if_util_list, debug=False):
    """
    Установить макросы commit rate по интерфейсам. Имена макросов с контекстом:
    {$IF.UTIL.MAX:"Ethernet51/1"} и {$IF.UTIL.WARN:"Ethernet51/1"} (90% для жёлтого на карте).
    new_if_util_list: список {"macro", "value", "type"} (macro начинается с {$IF.UTIL.MAX или {$IF.UTIL.WARN).
    Возврат (True, None) или (False, error_message).
    """
    to_delete = []
    for prefix in (MACRO_PREFIX, MACRO_PREFIX_WARN):
        result, err = zabbix_request(
            url, token, "usermacro.get",
            {"hostids": [hostid], "output": ["hostmacroid", "macro"], "search": {"macro": prefix}},
            debug=debug,
        )
        if err:
            return False, err
        to_delete.extend(m["hostmacroid"] for m in (result or []) if m.get("hostmacroid"))
    if to_delete:
        result_del, err_del = zabbix_request(url, token, "usermacro.delete", to_delete, debug=debug)
        if err_del:
            return False, err_del
    if not new_if_util_list:
        return True, None
    create_list = [
        {"hostid": str(hostid), "macro": entry["macro"], "value": entry["value"], "type": int(entry.get("type") or 0)}
        for entry in new_if_util_list
    ]
    result_c, err_c = zabbix_request(url, token, "usermacro.create", create_list, debug=debug)
    if err_c:
        return False, err_c
    return True, None


TRIGGER_TAG_SCRIPTS = {"tag": "scripts", "value": "automatization"}


def get_bits_received_item_key(url, token, hostid, iface_name, debug=False):
    """
    Найти ключ item'а «Bits received» для данного хоста и интерфейса (для выражения простого триггера).
    Сопоставление по ключу (Ethernet51/1 в []) или по имени item (Interface Ethernet51/1(...)).
    Возврат key_ или None.
    """
    res, err = zabbix_request(
        url, token, "item.get",
        {"hostids": [hostid], "output": ["key_", "name"], "search": {"name": "Bits received"}},
        debug=debug,
    )
    if err or not res:
        return None
    iface_norm = _normalize_interface_name((iface_name or "").strip())
    for it in res:
        key_str = it.get("key_") or ""
        name_str = it.get("name") or ""
        iface_from_k = _interface_from_key(key_str)
        iface_from_n = _interface_from_item_name(name_str)
        match = (iface_from_k and _normalize_interface_name(iface_from_k) == iface_norm) or (
            iface_from_n and _normalize_interface_name(iface_from_n) == iface_norm
        )
        if match:
            return key_str
    return None


# Приоритеты для отображения на карте: 2 = Warning (жёлтый), 4 = High (красный)
TRIGGER_PRIORITY_WARN = 2   # 90% — линк жёлтый
TRIGGER_PRIORITY_HIGH = 4   # 100% — линк красный


def ensure_simple_threshold_trigger(url, token, host_technical, hostid, iface_name, debug=False):
    """
    Создать/обновить простой триггер max(Bits received, 5m) > {$IF.UTIL.MAX:"iface"}.
    Линия порога на графике (Simple triggers) и красный линк на карте при 100%.
    Возврат (True, None) или (False, error_message).
    """
    key = get_bits_received_item_key(url, token, hostid, iface_name, debug=debug)
    if not key:
        return False, "не найден item Bits received для интерфейса {}".format(iface_name)
    macro_ref = _macro_name_for_interface(iface_name)
    expression = "max(/{}/{}, 5m)>{}".format(host_technical, key, macro_ref)
    description = "Interface {}: High bandwidth (threshold line)".format((iface_name or "").strip())
    existing, err = zabbix_request(
        url, token, "trigger.get",
        {"hostids": [hostid], "output": ["triggerid", "description", "priority"], "search": {"description": "threshold line"}},
        debug=debug,
    )
    if err:
        return False, err
    for t in (existing or []):
        if t.get("description") == description:
            tid = t.get("triggerid")
            if tid and str(t.get("priority", "0")) != str(TRIGGER_PRIORITY_HIGH):
                zabbix_request(url, token, "trigger.update", {"triggerid": tid, "priority": TRIGGER_PRIORITY_HIGH}, debug=debug)
            return True, None
    create_res, create_err = zabbix_request(
        url, token, "trigger.create",
        {
            "description": description,
            "expression": expression,
            "priority": TRIGGER_PRIORITY_HIGH,
            "tags": [TRIGGER_TAG_SCRIPTS],
        },
        debug=debug,
    )
    if create_err or not create_res or not create_res.get("triggerids"):
        return False, create_err or "trigger.create не вернул triggerid"
    return True, None


def ensure_simple_warn_trigger(url, token, host_technical, hostid, iface_name, debug=False):
    """
    Создать простой триггер 90%: max(Bits received, 5m) > {$IF.UTIL.WARN:"iface"}.
    На карте линк становится жёлтым при достижении 90% порога.
    Возврат (True, None) или (False, error_message).
    """
    key = get_bits_received_item_key(url, token, hostid, iface_name, debug=debug)
    if not key:
        return False, "не найден item Bits received для интерфейса {}".format(iface_name)
    macro_ref = _macro_name_warn_for_interface(iface_name)
    expression = "max(/{}/{}, 5m)>{}".format(host_technical, key, macro_ref)
    description = "Interface {}: High bandwidth (90%)".format((iface_name or "").strip())
    existing, err = zabbix_request(
        url, token, "trigger.get",
        {"hostids": [hostid], "output": ["triggerid", "description"], "search": {"description": "High bandwidth (90%)"}},
        debug=debug,
    )
    if err:
        return False, err
    if any(t.get("description") == description for t in (existing or [])):
        return True, None
    create_res, create_err = zabbix_request(
        url, token, "trigger.create",
        {
            "description": description,
            "expression": expression,
            "priority": TRIGGER_PRIORITY_WARN,
            "tags": [TRIGGER_TAG_SCRIPTS],
        },
        debug=debug,
    )
    if create_err or not create_res or not create_res.get("triggerids"):
        return False, create_err or "trigger.create не вернул triggerid"
    return True, None


def remove_threshold_items(url, token, hostid, debug=False):
    """
    Удалить на хосте все item'ы порога (ключ net.if.threshold[...]), больше не используются — линия рисуется простым триггером.
    Возврат (удалено_count, None) или (0, error_message).
    """
    res, err = zabbix_request(
        url, token, "item.get",
        {"hostids": [hostid], "output": ["itemid", "key_"], "search": {"key_": THRESHOLD_ITEM_KEY}},
        debug=debug,
    )
    if err:
        return 0, err
    ids = []
    for it in (res or []):
        key_str = it.get("key_") or ""
        if key_str.startswith(THRESHOLD_ITEM_KEY):
            itemid = it.get("itemid")
            if itemid:
                ids.append(str(itemid))
    if not ids:
        return 0, None
    _, del_err = zabbix_request(url, token, "item.delete", ids, debug=debug)
    if del_err:
        return 0, del_err
    return len(ids), None


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Синхронизировать {$IF.UTIL.MAX} в Zabbix из NetBox (commit rate контуров по кабелю к интерфейсу).",
    )
    parser.add_argument("-d", "--dry-ssh", default=None, metavar="FILE", help="dry-ssh.json: для кабеля на физике (напр. et-0/0/3) задать контекст макроса по логическому имени (ae5.0) для Zabbix")
    parser.add_argument("--dry-run", action="store_true", help="Не менять макросы в Zabbix, только вывести что бы установили")
    parser.add_argument("--debug", action="store_true", help="Отладочный вывод (статистика по NetBox, подстановка логических имён)")
    args = parser.parse_args()

    nb_url = os.environ.get("NETBOX_URL")
    nb_token = os.environ.get("NETBOX_TOKEN")
    tag = (os.environ.get("NETBOX_TAG") or "").strip() or "border"
    if not nb_url or not nb_token:
        print("Задайте NETBOX_URL и NETBOX_TOKEN", file=sys.stderr)
        sys.exit(1)

    zabbix_url, zabbix_token = _get_zabbix_url_token()
    if not zabbix_url or not zabbix_token:
        print("Задайте ZABBIX_URL и ZABBIX_TOKEN", file=sys.stderr)
        sys.exit(1)

    if not validate_zabbix_token(zabbix_url, zabbix_token, debug=args.debug):
        print("Неверный или просроченный ZABBIX_TOKEN", file=sys.stderr)
        sys.exit(1)

    nb = pynetbox.api(nb_url, token=nb_token)
    commit_rates = get_commit_rates_from_netbox(nb, tag, debug=args.debug)
    if not commit_rates:
        print(
            "В NetBox не найдено интерфейсов с circuit (termination A + кабель к dcim.interface). "
            "Проверьте NETBOX_TAG и запустите с --debug.",
            file=sys.stderr,
        )
        sys.exit(0)

    dry_ssh_path = getattr(args, "dry_ssh", None) or (DEFAULT_DRY_SSH if os.path.isfile(DEFAULT_DRY_SSH) else None)
    dry_ssh_devices = load_dry_ssh(dry_ssh_path) if dry_ssh_path else None
    if dry_ssh_path and dry_ssh_devices:
        commit_rates = apply_logical_context(commit_rates, dry_ssh_devices, debug=args.debug)
    elif dry_ssh_path and not dry_ssh_devices:
        if args.debug:
            print("dry-ssh не загружен (файл пустой или недоступен), контекст по имени из NetBox", file=sys.stderr)

    # Группируем по хосту для Zabbix
    host_to_iface_bps = {}
    for (dev_name, iface_name), bps in commit_rates.items():
        host_to_iface_bps.setdefault(dev_name, []).append((iface_name, bps))

    # Хосты в Zabbix по имени (host или name); техническое имя хоста для history.push
    hostnames = list(host_to_iface_bps.keys())
    result, err = zabbix_request(
        zabbix_url, zabbix_token, "host.get",
        {"output": ["hostid", "host", "name"], "filter": {"host": hostnames}},
        debug=args.debug,
    )
    if err:
        print("Zabbix host.get: {}".format(err), file=sys.stderr)
        sys.exit(1)
    hostid_by_host = {h["host"]: h["hostid"] for h in result}
    host_technical_by_hostid = {h["hostid"]: h["host"] for h in result}
    missing = set(hostnames) - set(hostid_by_host.keys())
    if missing:
        result2, err2 = zabbix_request(
            zabbix_url, zabbix_token, "host.get",
            {"output": ["hostid", "host", "name"], "filter": {"name": list(missing)}},
            debug=args.debug,
        )
        if not err2 and result2:
            for h in result2:
                hostid_by_host[h["name"]] = h["hostid"]
                host_technical_by_hostid[h["hostid"]] = h["host"]
        missing = set(hostnames) - set(hostid_by_host.keys())
    if missing:
        print("Хосты не найдены в Zabbix: {}".format(", ".join(sorted(missing))), file=sys.stderr)

    updated = 0
    for dev_name in hostnames:
        if dev_name not in hostid_by_host:
            continue
        hostid = hostid_by_host[dev_name]
        iface_bps_list = host_to_iface_bps[dev_name]
        new_if_util = []
        for iface_name, bps in iface_bps_list:
            new_if_util.append({
                "macro": _macro_name_for_interface(iface_name),
                "value": str(bps),
                "type": "0",
            })
            # 90% порог — для триггера «жёлтый линк» на карте
            new_if_util.append({
                "macro": _macro_name_warn_for_interface(iface_name),
                "value": str(int(bps * 0.9)),
                "type": "0",
            })

        if args.dry_run:
            print(
                "[dry-run] {} (hostid {}): макросы {} bps".format(
                    dev_name, hostid,
                    ", ".join("{}={}".format(c["macro"], c["value"]) for c in new_if_util),
                ),
                file=sys.stderr,
            )
            updated += 1
            continue
        ok, err = set_zabbix_host_if_util_macros(zabbix_url, zabbix_token, hostid, new_if_util, debug=args.debug)
        if not ok:
            print("Ошибка обновления макросов для {}: {}".format(dev_name, err or "usermacro"), file=sys.stderr)
            continue
        # Триггеры для линии порога на графике и цветов линков на карте (90% — жёлтый, 100% — красный)
        zabbix_host = host_technical_by_hostid.get(hostid) or dev_name
        for iface_name, _bps in iface_bps_list:
            ok_tr, err_tr = ensure_simple_threshold_trigger(
                zabbix_url, zabbix_token, zabbix_host, hostid, iface_name, debug=args.debug
            )
            if not ok_tr:
                print("  {}: триггер 100% — {}".format(iface_name, err_tr or "ошибка"), file=sys.stderr)
            ok_w, err_w = ensure_simple_warn_trigger(
                zabbix_url, zabbix_token, zabbix_host, hostid, iface_name, debug=args.debug
            )
            if not ok_w:
                print("  {}: триггер 90% — {}".format(iface_name, err_w or "ошибка"), file=sys.stderr)
        # Удалить старые item'ы порога net.if.threshold[...] (линия теперь от простого триггера)
        removed, rem_err = remove_threshold_items(zabbix_url, zabbix_token, hostid, debug=args.debug)
        if rem_err:
            print("  {}: удаление item'ов порога — {}".format(dev_name, rem_err), file=sys.stderr)
        msg = "OK: {} — установлено {} макросов (MAX+WARN 90%), триггеры 90%/100% для карты".format(dev_name, len(new_if_util))
        if removed:
            msg += ", удалено {} item'ов порога".format(removed)
        print(msg)
        updated += 1

    print("Готово: {} хостов обновлено, {} пар (интерфейс, commit rate) из NetBox.".format(
        updated, len(commit_rates)))


if __name__ == "__main__":
    main()
