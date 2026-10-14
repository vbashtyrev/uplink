# Отчёты и журналы

Здесь собраны отчёты, которые нужны для проверки области NetBox, плана
Zabbix и результатов запуска. Рабочие данные сюда не сохраняются.

## Перед записью

`--plan` сравнивает состояние Zabbix с ожидаемым и ничего не меняет. Полный
запуск сохраняет результат в `run_logs/<дата>_zabbix_plan.json` и
`run_logs/<дата>_zabbix_plan.txt`.

`--dry-run` запускает отдельную команду без записи. Этот режим есть, например,
у очистки Zabbix.

## Полный запуск

Команда:

```bash
python run_uplinks_full.py --plan --report /tmp/uplinks-run.log
```

Результаты:

- `run_logs/<дата>_run.log` — журнал шагов полного запуска;
- `run_logs/<дата>_debug.log` — подробный журнал;
- `run_logs/<дата>_zabbix_plan.json` — план в формате JSON;
- `run_logs/<дата>_zabbix_plan.txt` — тот же план в виде текста;
- файл из `--report` — дополнительная копия журнала полного запуска, а не
  отдельный файл плана.

Журнал полного запуска показывает шаги, состояние и ошибки. План объектов
хранится отдельно.

## Снимок области NetBox

Команды:

```bash
python netbox_uplinks_inventory.py --dry-run
python netbox_uplinks_inventory.py --json --dry-run > netbox_inventory.json
```

Отчёт показывает полные и неполные цепочки:

```text
Provider → Circuit → Termination A → Cable → Interface → Device
```

Снимок можно передавать следующим командам. Полный запуск создаёт его сам.

## План Zabbix

Команды:

```bash
python zabbix_uplinks_plan.py \
  --inventory-file netbox_inventory.json \
  --create-link-triggers \
  --text /tmp/uplinks-plan.txt \
  --output /tmp/uplinks-plan.json

python zabbix_uplinks_plan.py \
  --inventory-file netbox_inventory.json \
  --create-link-triggers \
  --json
```

План содержит макросы, триггеры, агрегаты, карту, дашборды и сервисы. Для
каждого раздела используются такие результаты:

- `create` — будет создано;
- `update` — будет изменено;
- `delete` — будет удалено;
- `unchanged` — уже совпадает;
- `skipped` — изменение подавлено из-за неоднозначного или неизвестного
  состояния;
- `not_evaluated` — сравнение не выполнялось.

## Отчёт доступности

Команды:

```bash
python zabbix_provider_sla.py --days 30
python zabbix_provider_sla.py \
  --inventory-file netbox_inventory.json \
  --days 30
```

Отчёт выводит отдельные таблицы по поставщикам и каналам с оплатой по схеме
`Burst`. Он использует историю событий Zabbix и показывает целевой уровень
доступности, длительность нарушений и итоговую оценку.

## Таблица карты связей в Zabbix

Команды:

```bash
python zabbix_map.py \
  --inventory-file netbox_inventory.json \
  --print-table

python zabbix_map.py \
  --inventory-file netbox_inventory.json \
  --zabbix \
  --print-table

python zabbix_map.py --export-map 10 > map_10.json
```

Таблица нужна для ручной проверки связей между устройствами, интерфейсами и
поставщиками.

## Предварительная очистка Zabbix

Команда:

```bash
python zabbix_uplinks_cleanup.py --dry-run
```

Отчёт показывает список объектов к удалению. Рабочую очистку запускают
отдельной командой после проверки этого списка.

## Где хранятся результаты

Каталог `run_logs/` содержит журналы запусков и не переносится в `reports/`.
Рабочие снимки, отчёты и журналы игнорируются Git. В репозиторий входит
только этот справочник.
