"""Example config for Uplinks automation (copy to uplinks_config.py and adjust)."""

# Map
MAP_NAME = "Uplinks"
MAP_ICON_HOST = 130
MAP_ICON_CLOUD = 4

# Dashboards
DASHBOARD_NAME = "Uplinks"
DASHBOARD_NAME_BY_LOCATION = "Uplinks (по локациям)"
DASHBOARD_NAME_BY_PROVIDER = "Uplinks по провайдерам"
# Optional static providers for summary dashboard (usually empty, providers come from NetBox).
PROVIDERS_FOR_SUMMARY = []

# Provider aggregate hosts
UPLINKS_AGGREGATE_HOST_PREFIX = "Uplinks "
UPLINKS_AGGREGATE_GROUP = "Uplinks"

# Thresholds (percent of commit rate)
THRESHOLD_PERCENT_WARN = 90
THRESHOLD_PERCENT_HIGH = 100

# Trigger tags and descriptions
TRIGGER_TAG_NAME = "scripts"
TRIGGER_TAG_VALUE = "automatization"
TRIGGER_FUNCTION_PERIOD = "15m"
TRIGGER_DESC_90_SUFFIX = "Commit rate exceeded ({}%)".format(THRESHOLD_PERCENT_WARN)
TRIGGER_DESC_100_SUFFIX = "Commit rate exceeded (100%)"
TRIGGER_DESC_SEARCH = "Commit rate exceeded ("
# SLA breach rule for provider aggregates: 100% for a sustained period
SLA_TRIGGER_FUNCTION_PERIOD = "1h"
# Per-link Burst: описание после «Interface {name}: » — только этот триггер с тегом sla=true
TRIGGER_DESC_SLA_BREACH_SUFFIX = "SLA breach: >= 100% of commit for {}".format(
    SLA_TRIGGER_FUNCTION_PERIOD
)
SLA_TRIGGER_TAG_NAME = "sla"
SLA_TRIGGER_TAG_VALUE = "true"
# SLA effective date (UTC timestamp), e.g. 2026-03-01 00:00:00 UTC
SLA_EFFECTIVE_DATE_UTC = 1772323200

# Link colors on map (hex without #)
LINK_COLOR_WARN = "DDBB00"
LINK_COLOR_HIGH = "DD0000"

# Macros (host level)
MACRO_PREFIX_MAX = "{$IF.UTIL.MAX"
MACRO_PREFIX_WARN = "{$IF.UTIL.WARN"

# Legacy threshold item key (cleanup)
THRESHOLD_ITEM_KEY = "net.if.threshold"

# Uplink VRF name for stats collection (per-device)
UPLINK_VRF_NAME = "internet"

# NetBox automation tag for all created objects
NETBOX_AUTOMATION_TAG = TRIGGER_TAG_VALUE

