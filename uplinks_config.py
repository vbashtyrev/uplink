# -*- coding: utf-8 -*-
"""
Настраиваемые названия и значения для создания элементов Zabbix (карта, дашборды, триггеры, теги)
и для сбора данных с устройств (VRF uplink'ов). Меняйте под своё окружение — скрипты uplinks
(uplinks_stats, zabbix_map, zabbix_sync_commit_rate, zabbix_uplinks_dashboard, zabbix_uplinks_cleanup и др.)
используют эти константы.
"""

# --- Карта ---
# Название карты в Zabbix (Monitoring → Maps)
MAP_NAME = "Uplinks"

# Иконки элементов карты (imageid в Администрирование → Изображения)
MAP_ICON_HOST = 130   # хосты, напр. Router_symbol_(64)
MAP_ICON_CLOUD = 4    # провайдеры, напр. Cloud


# --- Дашборды ---
# Название основного дашборда с графиками uplink
DASHBOARD_NAME = "Uplinks"
# Название дашборда «по локациям» (вкладки = локации)
DASHBOARD_NAME_BY_LOCATION = "Uplinks (по локациям)"


# --- Пороги по загрузке (триггеры и макросы) ---
# Проценты от commit rate: при достижении WARN — жёлтый линк на карте, при HIGH — красный и линия порога на дашборде
THRESHOLD_PERCENT_WARN = 90   # порог предупреждения (Warning)
THRESHOLD_PERCENT_HIGH = 100  # порог высокий (High), линия на графике

# --- Триггеры: тег и описания ---
# Тег Zabbix для «наших» триггеров (по нему cleanup находит и удаляет их)
TRIGGER_TAG_NAME = "scripts"
TRIGGER_TAG_VALUE = "automatization"

# Период для max() в выражении триггера (max(item, period) > макрос). Формат Zabbix: 5m, 15m, 1h и т.д.
TRIGGER_FUNCTION_PERIOD = "15m"

# Суффиксы описания триггера (полное: "Interface <имя>: " + суффикс)
# Суффикс WARN строится из THRESHOLD_PERCENT_WARN; HIGH — фиксированный (линия порога на дашборде)
TRIGGER_DESC_90_SUFFIX = "High bandwidth ({}%)".format(THRESHOLD_PERCENT_WARN)
TRIGGER_DESC_100_SUFFIX = "High bandwidth (threshold line)"
# Подстрока для поиска триггеров в API (search по description)
TRIGGER_DESC_SEARCH = "High bandwidth ("

# Цвета линков на карте при срабатывании триггеров (hex без #)
LINK_COLOR_WARN = "DDBB00"   # 90% — жёлтый
LINK_COLOR_HIGH = "DD0000"   # 100% — красный


# --- Макросы хоста ---
# Префиксы имён макросов (полное: префикс + ':"<интерфейс>"}' )
MACRO_PREFIX_MAX = "{$IF.UTIL.MAX"   # 100% порог
MACRO_PREFIX_WARN = "{$IF.UTIL.WARN"  # 90% порог


# --- Item'ы (исторические) ---
# Ключ старых item'ов порога — удаляются при синке и в cleanup
THRESHOLD_ITEM_KEY = "net.if.threshold"


# --- Сбор данных с устройств (uplinks_stats) ---
# VRF / routing-instance для uplink'ов: при сборе с устройств в поле ip_vrf учитывается только этот VRF (Juniper: routing-instance, Arista: show vrf)
UPLINK_VRF_NAME = "internet"

# --- NetBox / circuits ---
# Имя/slug тега NetBox для объектов, созданных автоматизацией uplinks
NETBOX_AUTOMATION_TAG = TRIGGER_TAG_VALUE
