

```bash
cd /path/to/uplinks
source .venv/bin/activate

export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="..."
export ZABBIX_URL="https://zabbix.example.com"
export ZABBIX_TOKEN="..."

```

## Рабочий режим

Провайдер, Circuit, терминация и кабель создаются человеком в NetBox.
Обычный полный запуск только читает эту цепочку и настраивает Zabbix.
Текущие показатели устройств получает другой проект.

Проверка NetBox без изменений:

```bash
python netbox_uplinks_inventory.py --dry-run
python netbox_uplinks_inventory.py --json --dry-run
```

Обычный запуск не создаёт, не изменяет и не удаляет объекты NetBox.

Чтобы канал попал в мониторинг, в NetBox должно быть заполнено:

- у Provider — при необходимости поля `aggregate_limit_gbps` (общий лимит в
  Гбит/с) и `slo_percent` (целевой уровень доступности);
- у Circuit — тип `Uplink`, встроенный статус `Active`, поле
  `billing_model` и стандартное поле
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

Старый автоматический путь удалён. Provider, Circuit, Termination и Cable
создаются вручную в NetBox.


---



```bash
python run_uplinks_full.py

python run_uplinks_full.py
```

Полный запуск в рабочем режиме:

```bash
python run_uplinks_full.py
```

Он использует:

```text
NetBox inventory → Zabbix
```


Та же цепочка по шагам, если нужно выполнить её вручную:

```bash
python netbox_uplinks_inventory.py --json --dry-run > netbox_inventory.json

python zabbix_sync_commit_rate.py --inventory-file netbox_inventory.json --create-link-triggers
python zabbix_provider_aggregate.py --inventory-file netbox_inventory.json
python zabbix_map.py --inventory-file netbox_inventory.json --zabbix --update-map
python zabbix_uplinks_dashboard.py --inventory-file netbox_inventory.json

python zabbix_provider_services.py --parent-service 'Uplinks providers'
```

---

Опрос устройств и сверка их данных выполняются отдельно другим проектом.


---

Данные с устройств получает и передаёт другой проект.

Опрос устройств и сверка их данных выполняются отдельно другим проектом.


---






---

Проверка без записи:

```bash
python run_uplinks_full.py --plan --report uplinks_plan_report.txt
python zabbix_uplinks_plan.py --inventory-file netbox_inventory.json
```

План проверяет не только макросы и служебные триггеры. Он также показывает
практические изменения для агрегатов, карты, дашбордов и сервисов с SLA
(целевым уровнем доступности). Координаты карты и полное содержимое виджетов
побитово не сравниваются. При ошибке чтения удаление и полная перезапись
объектов подавляются и отмечаются как `skipped` или `not_evaluated`.

Ключ `--dry-run` у `zabbix_sync_commit_rate.py` нельзя совмещать с
`--delete-link-triggers` и `--delete-util-triggers`.

---




```bash

python zabbix_sync_commit_rate.py -d dry-ssh.json

python zabbix_sync_commit_rate.py -d dry-ssh.json --create-link-triggers
```

---

Работа по одной площадке выполняется фильтрацией данных в NetBox. Отдельного
ключа `--location` в полном запуске нет.

---
