# Unraid SMART Cache Exporter

A lightweight Prometheus exporter for Unraid that reads Unraid-managed SMART cache files, disk state files, and emhttpd lifecycle events from syslog.

## Why this exists

- Reads cached files from Unraid (`/var/local/emhttp/smart/*` and `/var/local/emhttp/disks.ini`).
- Parses Unraid `emhttpd` disk lifecycle events from syslog (`spinning up`, `spinning down`, `read SMART`).
- Does **not** execute `smartctl` itself.
- Designed to reduce scrape-induced disk wakeups while still surfacing actionable state-change signals.

## Exposed endpoints

- `GET /metrics`
- `GET /healthz`

Default listen: `0.0.0.0:9903`

## Container image pattern

This image uses your `python-container-builder` + distroless runtime paradigm:

- Build stage: `ghcr.io/jski/python-container-builder:3.12`
- Runtime stage: `gcr.io/distroless/python3-debian12:nonroot`

The build stage provides Python and a pre-created virtualenv (`/.venv`), and the runtime stage stays minimal and shell-free for production use.

## Quick start (Unraid compose stack)

1. Copy this folder to your Unraid appdata/project path.
2. Use `examples/docker-compose.unraid.yml` (includes image + local build fallback + required mounts).
3. Start stack.
4. Verify:

```bash
curl -sS http://127.0.0.1:9903/healthz
curl -sS http://127.0.0.1:9903/metrics | head -n 80
```

## Alloy/Prometheus scrape target

Use target:

- `unraid:9903` (if your Alloy can resolve host alias), or
- `<unraid-ip>:9903`

## Key metrics

SMART + disk state:

- `unraid_smart_attr_raw{disk,attr_id,attr_name}`
- `unraid_smart_temperature_celsius{disk}`
- `unraid_smart_pending_sectors{disk}`
- `unraid_smart_offline_uncorrectable{disk}`
- `unraid_smart_reallocated_sectors{disk}`
- `unraid_disk_info{disk,device,status,disk_type,transport}`
- `unraid_disk_temp_celsius{disk}`

Event/state-change metrics:

- `unraid_disk_event_total{disk,device,event,event_source}`
- `unraid_disk_last_event_timestamp_seconds{disk,device,event,event_source}`
- `unraid_disk_last_spinup_timestamp_seconds{disk}`
- `unraid_disk_last_spindown_timestamp_seconds{disk}`
- `unraid_disk_last_smart_read_timestamp_seconds{disk}`
- `unraid_disk_spin_state{disk,device,state_source,confidence}` (`1=up`, `0=down`, `-1=unknown`)
- `unraid_disk_spin_state_last_change_timestamp_seconds{disk,device,state_source,confidence}`

Exporter self-health metrics:

- `unraid_exporter_log_parse_errors_total`
- `unraid_exporter_log_scan_errors_total`
- `unraid_exporter_last_successful_log_scan_timestamp_seconds`
- `unraid_exporter_log_cursor_offset_bytes`
- `unraid_exporter_log_lag_seconds`
- `unraid_exporter_inferred_transitions_total`
- `unraid_exporter_state_persist_ok`
- `unraid_exporter_state_persist_errors_total`
- `unraid_exporter_last_state_persist_error_timestamp_seconds`

## Environment variables

- `LISTEN_HOST` (default `0.0.0.0`)
- `LISTEN_PORT` (default `9903`)
- `SMART_DIR` (default `/var/local/emhttp/smart`)
- `DISKS_INI` (default `/var/local/emhttp/disks.ini`)
- `SYSLOG_PATH` (default `/var/log/syslog`)
- `SYSLOG_TIMEZONE` (optional explicit IANA timezone, e.g. `America/New_York`)
- `STATE_PATH` (default `/var/lib/unraid-smart-cache-exporter/state.json`)
- `SYSLOG_INITIAL_TAIL_BYTES` (default `4194304`)
- `EXCLUDE_NON_PRESENT` (default `false`; set `true` to omit `DISK_NP*` / empty-device slots from disk metrics)

## Notes

- SMART freshness depends on Unraid's own SMART update cadence.
- Syslog lifecycle parsing tracks a persistent cursor in `STATE_PATH` so counters do not double-count on every scrape.
- Syslog timestamps do not include timezone. Resolution priority is:
  1. explicit `SYSLOG_TIMEZONE` env
  2. `TZ` env
  3. host timezone auto-detect (`/host/etc/TZ`, `/host/etc/timezone`, `/host/etc/localtime`, plus local fallbacks)
  4. container local timezone
- Event provenance is explicit in metric labels:
  - `event_source="explicit"` for parsed emhttpd lifecycle logs.
  - `event_source="inferred"` for fallback spin-up transitions inferred from disk counter deltas after a known down state.

## Dashboard and Alerting Assets

Reusable assets are included in this repo:

- Grafana dashboard: `examples/grafana/unraid-smart-cache-overview.json`
- Prometheus alert rules (for Alertmanager delivery): `examples/alerts/prometheus-unraid-smart-cache.rules.yml`

Usage:

1. Import the dashboard JSON in Grafana (`Dashboards -> Import`).
2. Add/load the Prometheus rule file into your Prometheus `rule_files` configuration.
3. Ensure Prometheus is configured with Alertmanager; alerts from this rule group route there automatically.

## CI

GitHub Actions workflows are included:

- `.github/workflows/ci.yml` for lint + unit tests.
- `.github/workflows/docker-publish.yml` for PR image builds and no-rebuild promotion on `main`/release tags.
