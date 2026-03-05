# Unraid SMART Cache Exporter

A lightweight Prometheus exporter for Unraid that reads Unraid-managed SMART cache files and disk state files.

## Why this exists

- Reads cached files from Unraid (`/var/local/emhttp/smart/*` and `/var/local/emhttp/disks.ini`).
- Does **not** execute `smartctl` itself.
- Designed to reduce risk of scrape-induced disk wakeups.

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
2. Use `examples/docker-compose.unraid.yml` (or merge service into your existing stack).
3. Start stack.
4. Verify:

```bash
curl -sS http://127.0.0.1:9903/healthz
curl -sS http://127.0.0.1:9903/metrics | head -n 40
```

## Alloy/Prometheus scrape target

Use target:

- `unraid:9903` (if your Alloy can resolve host alias), or
- `<unraid-ip>:9903`

Example key metrics:

- `unraid_smart_attr_raw{disk,attr_id,attr_name}`
- `unraid_smart_temperature_celsius{disk}`
- `unraid_smart_pending_sectors{disk}`
- `unraid_smart_offline_uncorrectable{disk}`
- `unraid_smart_reallocated_sectors{disk}`
- `unraid_disk_info{disk,device,status,disk_type,transport}`
- `unraid_disk_temp_celsius{disk}`

## Environment variables

- `LISTEN_HOST` (default `0.0.0.0`)
- `LISTEN_PORT` (default `9903`)
- `SMART_DIR` (default `/var/local/emhttp/smart`)
- `DISKS_INI` (default `/var/local/emhttp/disks.ini`)

## Notes

- Freshness depends on Unraid's own SMART update cadence.
- If you change Unraid polling behavior, this exporter reflects it automatically.

## CI

GitHub Actions workflows are included:

- `.github/workflows/ci.yml` for lint + unit tests.
- `.github/workflows/docker-publish.yml` for PR image builds and no-rebuild promotion on `main`/release tags.
