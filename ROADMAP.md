# Roadmap

Планы доработок проекта uplinks (не обязательно в указанном порядке).

## Ближайшее

- [x] CI: GitHub Actions + pytest на push/PR
- [x] Единый env-loader (`env_urls.load_env_file` с `overwrite=`)
- [x] Исправить/актуализировать runbook (`MANUAL.md`) — порядок Zabbix-шагов

## NetBox

- [ ] ~~**Tenancy для circuits**~~ — отложено (по необходимости)
- [x] Пакетная структура: `netbox_checks`, `netbox_create_circuits` → `uplinks/netbox/`

## Общие данные

- [x] `uplinks/data.py` — `load_devices_json`, `load_description_map`, константы путей

## Zabbix

- [x] Вынести JSON-RPC клиент и кэш из `zabbix_map.py` в `uplinks/zabbix/client.py`
- [ ] Опционально: единый CLI (`uplinks run`, `uplinks sync`, …) с сохранением текущих скриптов как thin wrappers

## Качество

- [ ] Консолидация тестов (`*_wave2.py`, `*_85.py` → доменные файлы)
- [ ] Опционально: ruff/mypy в CI

## Grafana

- [ ] Решить судьбу экспериментов MapGL / business charts (были в ветке `feature/grafana-uplinks`, не влиты в main)
- [x] `grafana_uplinks_graph.py` вне `run_uplinks_full.py` — осознанно; см. COMMANDS.md / README
