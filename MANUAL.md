# Краткое руководство

## Подготовка

```bash
cd /path/to/zabbix-uplinks
source .venv/bin/activate

export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="..."
export ZABBIX_URL="https://zabbix.example.com"
export ZABBIX_TOKEN="..."
```

Параметры карты, дашбордов, порогов и целевого уровня доступности находятся
в `uplinks_config.py`. В репозитории есть безопасные значения по умолчанию.
Секреты в этот файл не записываются.

## Что должно быть в NetBox

Провайдер, канал, терминация и кабель создаются вручную. В мониторинг попадает
только цепочка:

```text
Provider → Circuit → Termination A → Cable → Interface → Device
```

У канала должны быть:

- тип `Uplink`;
- встроенный статус `Active`;
- поле `billing_model`;
- `commit_rate` в Кбит/с, кроме `FlatAggCap`;
- полная кабельная цепочка до интерфейса устройства.

У конечного устройства должен быть тег `border`. Общий лимит поставщика
задаётся в `aggregate_limit_gbps`, а индивидуальный уровень доступности —
в `slo_percent`. Если `slo_percent` не заполнен, используется
`PROJECT_PROVIDER_SLO_PERCENT`.

Проверить цепочки без записи:

```bash
python netbox_uplinks_inventory.py --json --dry-run > netbox_inventory.json
```

## Полный запуск

```bash
python run_uplinks_full.py
```

Полный запуск использует один снимок NetBox и последовательно выполняет:

1. макросы и Burst-триггеры;
2. агрегаты поставщиков;
3. карту;
4. дашборды;
5. сервисы и SLA (целевой уровень доступности).

Предварительный отчёт:

```bash
python run_uplinks_full.py --plan --report uplinks_plan_report.txt
```

Режим `--plan` ничего не записывает. Burst-триггеры в нём сравниваются по
описанию, выражению, приоритету, тегам и зависимости. Неоднозначные совпадения
помечаются как `skipped`, без создания дубликата.

## Пошаговый запуск

```bash
python zabbix_sync_commit_rate.py \
  --inventory-file netbox_inventory.json \
  --create-link-triggers
python zabbix_provider_aggregate.py \
  --inventory-file netbox_inventory.json
python zabbix_map.py \
  --inventory-file netbox_inventory.json \
  --zabbix --update-map
python zabbix_uplinks_dashboard.py \
  --inventory-file netbox_inventory.json
python zabbix_provider_services.py \
  --inventory-file netbox_inventory.json
```

Если снимок NetBox неполный, операции удаления и полные перезаписи
подавляются. Переданный снимок не дополняется повторным запросом к NetBox.

## Сбор по SSH и сверка интерфейсов

Эти команды являются отдельным ручным инструментом:

```bash
python uplinks_stats.py --fetch --json > dry-ssh.json
python netbox_checks.py -f dry-ssh.json --all --mt-ref --show-change
python netbox_checks.py -f dry-ssh.json --mediatype --mt-ref --apply
```

`dry-ssh.json` можно передать синхронизации через `-d/--dry-ssh` для
сопоставления физического и логического имени интерфейса. Скорость,
провайдер, область мониторинга и интерфейсы для утилизации берутся из NetBox.

## Карта, сервисы и отчёт SLA

```bash
python zabbix_map.py \
  --inventory-file netbox_inventory.json \
  --zabbix --print-table
python zabbix_map.py --create-map
python zabbix_provider_services.py \
  --inventory-file netbox_inventory.json \
  --parent-service "Uplinks providers"
python zabbix_provider_sla.py \
  --inventory-file netbox_inventory.json \
  --days 30
```

## Удаление объектов Zabbix

```bash
python zabbix_uplinks_cleanup.py --dry-run
python zabbix_uplinks_cleanup.py
```

Сначала используйте `--dry-run`, то есть просмотр без изменений. Скрипты
не создают, не изменяют и не удаляют объекты NetBox.
