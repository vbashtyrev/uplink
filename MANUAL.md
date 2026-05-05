# Минимануал: частые операции (uplinks)

Перед любым запуском: активировать venv и выставить переменные окружения.

```bash
cd /path/to/uplinks
source .venv/bin/activate

# Обязательно
export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="..."
export ZABBIX_URL="https://zabbix.example.com"
export ZABBIX_TOKEN="..."

# Для сбора с устройств (шаг 1)
export SSH_USERNAME="..."
export SSH_PASSWORD="..."
# опционально: NETBOX_TAG, SSH_HOST_SUFFIX, PARALLEL_DEVICES
```

Файл **`uplinks_config.py`** должен существовать (скопировать из `uplinks_config.example.py` при первом запуске).

---

## 1. Обновление commit rates (полный цикл)

Когда изменили оплаченные скорости по линкам или добавили новые — нужно обновить `commit_rates.json`, NetBox circuits и Zabbix (макросы, пороги на графиках).
Для uplinks используются макросы `{$UPLINK.BPS.MAX/WARN}` (bps), чтобы не конфликтовать со стандартными шаблонами Zabbix на `{$IF.UTIL.*}` (проценты).

**Вариант А — одной командой (рекомендуется):**

```bash
# Всё цепочкой: сбор → commit_rates → NetBox → Zabbix (карта, дашборды, агрегаты)
python run_uplinks_full.py

# Принудительно пересобрать dry-ssh.json (игнорировать кэш 24ч)
python run_uplinks_full.py --refresh

# Без опроса устройств (уже есть актуальный dry-ssh.json)
python run_uplinks_full.py --no-fetch
```

**Вариант Б — по шагам:**

```bash
# 1) Собрать данные с устройств
python uplinks_stats.py --fetch --json > dry-ssh.json

# 2) Сгенерировать/обновить commit_rates.json (существующие _provider_limits и _provider_sla сохраняются)
python generate_commit_rates.py -f dry-ssh.json -o commit_rates.json

# 3) При необходимости вручную поправить commit_rates.json (commit_rate_gbps, circuit_id, _provider_limits)

# 4) Circuits в NetBox
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json

# 5) Макросы и пороги в Zabbix (откуда берутся линии на графиках)
python zabbix_sync_commit_rate.py -d dry-ssh.json

# 6) Карта и дашборды Zabbix
python zabbix_map.py -f dry-ssh.json --zabbix --update-map
python zabbix_provider_aggregate.py -f commit_rates.json -d dry-ssh.json
python zabbix_uplinks_dashboard.py -f dry-ssh.json

# 7) Сервисы и SLA по провайдерам (если используете)
python zabbix_provider_services.py -f commit_rates.json
```

---

## 2. Только поменяли commit_rates.json вручную

Если `dry-ssh.json` и список линков не менялись, а изменили только значения в `commit_rates.json` (commit_rate_gbps, _provider_limits и т.д.):

```bash
# NetBox: обновить commit rate у контуров
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json

# Zabbix: подтянуть макросы и пороги на хостах
python zabbix_sync_commit_rate.py -d dry-ssh.json

# Агрегаты по провайдерам (если правили _provider_limits)
python zabbix_provider_aggregate.py -f commit_rates.json -d dry-ssh.json
```

Карту и дашборды можно не трогать — они уже привязаны к тем же линкам; пороги на графиках обновятся за счёт макросов.

---

## 3. Добавили новый линк или новое устройство

1. Убедиться, что в NetBox у устройства есть тег (например `NETBOX_TAG=border`), у интерфейса в description есть строка `Uplink:`.
2. Прогнать полный цикл (см. пункт 1), чтобы в `dry-ssh.json` появился новый хост/интерфейс.
3. В `generate_commit_rates.py` новые пары (хост–интерфейс) попадут в `commit_rates.json` с `commit_rate_gbps: null` — заполнить вручную, затем шаги 4–7 из пункта 1.

Либо одной командой:

```bash
python run_uplinks_full.py --refresh
# после чего дописать в commit_rates.json commit_rate_gbps для новых линков и снова:
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json
python zabbix_sync_commit_rate.py -d dry-ssh.json
python zabbix_map.py -f dry-ssh.json --zabbix --update-map
python zabbix_provider_aggregate.py -f commit_rates.json -d dry-ssh.json
python zabbix_uplinks_dashboard.py -f dry-ssh.json
```

---

## 4. Только обновить карту и дашборды в Zabbix

Данные с устройств и commit_rates не трогали; нужно только перерисовать карту/дашборды (например после правок в Zabbix или description_to_name.json):

```bash
python zabbix_map.py -f dry-ssh.json --zabbix --update-map
python zabbix_uplinks_dashboard.py -f dry-ssh.json
```

При необходимости обновить и сводный дашборд по провайдерам — он создаётся той же командой при наличии `--dashboard-by-provider` и списка провайдеров (см. COMMANDS.md).

---

## 5. Проверка без записи (dry-run)

Перед применением изменений в NetBox или Zabbix можно посмотреть, что будет сделано:

```bash
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json --dry-run
python zabbix_sync_commit_rate.py -d dry-ssh.json --dry-run
python zabbix_uplinks_cleanup.py --dry-run   # что удалится в Zabbix
python netbox_uplinks_cleanup.py --dry-run    # что удалится в NetBox
```

---

## 7. Если раньше были записаны `{$IF.UTIL.*}` в bps (миграция)

Симптом: шаблонный триггер high bandwidth не срабатывает, хотя трафик высокий.

Нужно вернуть `{$IF.UTIL.*}` под шаблон и оставить bps только в `{$UPLINK.BPS.*}`:

```bash
# 1) Удалить host-level {$IF.UTIL.*} на uplink-хостах (разовый шаг)
# (можно через UI или вашим рабочим скриптом/запросом API)

# 2) Пересоздать актуальные макросы uplinks
python zabbix_sync_commit_rate.py -d dry-ssh.json

# 3) Если есть Burst — обновить per-link триггеры под {$UPLINK.BPS.*}
python zabbix_sync_commit_rate.py -d dry-ssh.json -f commit_rates.json --create-link-triggers
```

---

## 8. Одна локация

Ограничить операции одной площадкой (первый сегмент hostname, например ALA):

```bash
python run_uplinks_full.py --no-fetch --location ALA
# или только circuits:
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json --location ALA
```

---

Полный список аргументов и переменных — в **COMMANDS.md** и **README.md**.
