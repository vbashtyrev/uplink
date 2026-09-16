

```bash
cd /path/to/uplinks
source .venv/bin/activate

export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="..."
export ZABBIX_URL="https://zabbix.example.com"
export ZABBIX_TOKEN="..."

export SSH_USERNAME="..."
export SSH_PASSWORD="..."
```

## Рабочий режим

Провайдер, контур, терминация и кабель создаются человеком в NetBox.
Обычный полный запуск только читает эту цепочку, получает данные с
оборудования и обновляет существующие поля интерфейса.

Проверка NetBox без изменений:

```bash
python netbox_uplinks_inventory.py --dry-run
python netbox_uplinks_inventory.py --json --dry-run
```

Обычный запуск не создаёт и не удаляет провайдеров, контуров, терминаций и
кабелей. Для интерфейсов используется безопасный режим `--existing-only`:
отсутствующие интерфейсы, MAC и IP не создаются, а перепривязки не выполняются.
Обратите внимание: существующие поля интерфейсов (описание, тип, скорость,
режим дуплекса, MAC, MTU, IP) обычный запуск в NetBox всё же обновляет —
неизменными остаются только объекты подключения. Чтобы не записывать в NetBox
ничего, используйте `--no-netbox-apply` или режим `--plan`.

Чтобы канал попал в мониторинг, в NetBox должно быть заполнено:

- у Provider — при необходимости поля `aggregate_limit_gbps` (общий лимит в
  Гбит/с) и `slo_percent` (целевой уровень доступности);
- у Circuit — встроенный статус `Active`, тег `uplinks`, поле
  `uplinks_circuit_lifecycle=active`, поле `billing_model` и стандартное поле
  `commit_rate` в Кбит/с (последнее не нужно только при
  `billing_model=FlatAggCap`, когда лимит общий у поставщика);
- терминация стороны **A** и кабель от неё до интерфейса устройства (кабель
  через проходные порты оптического шкафа тоже подходит);
- у устройства, где кабель заканчивается, — тег `border`.

Полный список с пояснениями — в README, раздел «Что человек заводит в NetBox».

Если чтение NetBox не удалось полностью — отказано в доступе, не получен
список поставщиков или часть запросов вернула ошибку — скрипты ничего не
удаляют в Zabbix, не переписывают целиком макросы хостов, дашборды, связи
схемы и формулы агрегатов, и завершаются с кодом ошибки. Схема при этом не
обновляется совсем. Так неполные данные не приводят к потере настроек.
Подробнее — в README, раздел про `zabbix_sync_commit_rate.py`.

Переходные ключи, нужные только на время миграции:

- `--legacy-commit-rates-fallback` у `zabbix_sync_commit_rate.py`,
  `zabbix_provider_aggregate.py`, `zabbix_provider_services.py` и
  `zabbix_provider_sla.py` — читать старый `commit_rates.json`;
- `--legacy-provider-filter` у `zabbix_map.py`,
  `zabbix_provider_aggregate.py` и `zabbix_uplinks_dashboard.py` — отбирать
  интерфейсы по тексту `Uplink:` в описании вместо области мониторинга NetBox.

Старый автоматический путь доступен только явно:

```bash
python run_uplinks_full.py --auto
```

Не запускайте `--auto` на рабочем NetBox: старый код может заменить кабель.


---



```bash
python run_uplinks_full.py

python run_uplinks_full.py --refresh

python run_uplinks_full.py --no-fetch
```

Полный запуск в рабочем режиме:

```bash
python run_uplinks_full.py
```

Он использует:

```text
NetBox inventory (netbox_inventory.json) → SSH только по найденным устройствам
→ existing-only checks → Zabbix
```


Та же цепочка по шагам, если нужно выполнить её вручную. Файл
`commit_rates.json` в ней не участвует:

```bash
python netbox_uplinks_inventory.py --json --dry-run > netbox_inventory.json

python uplinks_stats.py --fetch --json --inventory-file netbox_inventory.json > dry-ssh.json

python netbox_checks.py -f dry-ssh.json --all --no-tx-power --scope-to-file --mt-ref --existing-only --apply

python zabbix_sync_commit_rate.py -d dry-ssh.json --create-link-triggers

python zabbix_provider_aggregate.py -d dry-ssh.json
python zabbix_map.py -f dry-ssh.json --zabbix --update-map
python zabbix_uplinks_dashboard.py -f dry-ssh.json

python zabbix_provider_services.py --parent-service 'Uplinks providers'
```

---

Только настройка Zabbix по уже собранным данным, без опроса устройств:

```bash
python zabbix_sync_commit_rate.py -d dry-ssh.json --create-link-triggers

python zabbix_provider_aggregate.py -d dry-ssh.json
```


---

Обновить данные с устройств и пересобрать мониторинг:

```bash
python run_uplinks_full.py --refresh
```

То же по шагам:

```bash
python uplinks_stats.py --fetch --json --inventory-file netbox_inventory.json > dry-ssh.json
python zabbix_sync_commit_rate.py -d dry-ssh.json --create-link-triggers
python zabbix_provider_aggregate.py -d dry-ssh.json
python zabbix_map.py -f dry-ssh.json --zabbix --update-map
python zabbix_uplinks_dashboard.py -f dry-ssh.json
```


---



```bash
python zabbix_provider_aggregate.py -d dry-ssh.json
python zabbix_map.py -f dry-ssh.json --zabbix --update-map
python zabbix_uplinks_dashboard.py -f dry-ssh.json
```


---

Проверка без записи:

```bash
python run_uplinks_full.py --plan --from-file --report uplinks_plan_report.txt
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json --dry-run
python zabbix_sync_commit_rate.py -d dry-ssh.json --dry-run
```

Ключ `--dry-run` у `zabbix_sync_commit_rate.py` нельзя совмещать с
`--delete-link-triggers` и `--delete-util-triggers`.

---




```bash

python zabbix_sync_commit_rate.py -d dry-ssh.json

python zabbix_sync_commit_rate.py -d dry-ssh.json --create-link-triggers
```

---

Работа по одной площадке. У полного запуска ключ `--location` допустим только
вместе с `--auto`; у проверки контуров он работает и без записи:

```bash
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json --location ALA
```

---
