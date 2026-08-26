# solis

Monitoring for a Solis 6 kW hybrid inverter reached over Modbus through a
Solarman/IGEN WiFi logger. **Everything network-facing here is read-only** —
the only write path is the `control.py` CLI, run by hand.

Three ways to consume it:

| Route | What | For |
|---|---|---|
| `custom_components/solis_solarman` | HACS custom integration: native HA sensors, config-flow setup | Home Assistant (recommended) |
| `Dockerfile` / `docker-compose.yml` | `solis_api.py` as a container serving `/api/state` + `/api/health` | HA `rest:` sensors ([homeassistant.md](homeassistant.md)) or anything else that reads JSON |
| `settings_dash.py` | Local browser dashboard (tiles, forecast, charge window timeline) | a laptop on the LAN |

## HACS install

HACS → Custom repositories → `davens/solis` (type: Integration) → install →
restart HA → Settings → Devices & services → Add integration → *Solis Solarman
(read-only)*. It asks for the logger IP and serial; nothing else. The logger
accepts **one Modbus session at a time** — stop any other client first.

## Also here

- `solar_forecast.py` — five-model Open-Meteo ensemble calibrated against
  recorded generation; answers "will tomorrow's sun refill the battery?"
- `control.py` — dry-run-by-default register writes, with hard refusal of the
  DNO grid-protection range.
- `CLAUDE.md` — the live-verified register map and the reasoning behind every
  non-obvious decision.
