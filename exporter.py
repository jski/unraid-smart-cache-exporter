#!/usr/bin/env python3
"""Unraid SMART cache exporter.

Reads Unraid-managed SMART cache files from /var/local/emhttp/smart and disk
state from /var/local/emhttp/disks.ini, then exposes Prometheus metrics.

This exporter does not run smartctl directly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SMART_ATTR_RE = re.compile(r"^\s*(\d+)\s+([A-Za-z0-9_\-]+)\s+")
SECTION_RE = re.compile(r'^\["?([^"\]]+)"?\]$')
KEY_VALUE_RE = re.compile(r'^(\w+)="(.*)"$')
INT_RE = re.compile(r"-?\d+")
SYSLOG_EVENT_RE = re.compile(
    r"^(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+"
    r"(?P<hms>\d{2}:\d{2}:\d{2})\s+\S+\s+emhttpd:\s+"
    r"(?P<event>spinning down|spinning up|read SMART)\s+/dev/(?P<device>sd[a-z])\b"
)


@dataclass
class SmartAttribute:
    attr_id: int
    name: str
    value: int
    worst: int
    threshold: int
    raw: Optional[int]


@dataclass
class SmartSnapshot:
    disk: str
    mtime: float
    attrs: Dict[int, SmartAttribute]


def _env(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value if value else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


SMART_DIR = Path(_env("SMART_DIR", "/var/local/emhttp/smart"))
DISKS_INI = Path(_env("DISKS_INI", "/var/local/emhttp/disks.ini"))
SYSLOG_PATH = Path(_env("SYSLOG_PATH", "/var/log/syslog"))
STATE_PATH = Path(_env("STATE_PATH", "/var/lib/unraid-smart-cache-exporter/state.json"))
SYSLOG_INITIAL_TAIL_BYTES = _env_int("SYSLOG_INITIAL_TAIL_BYTES", 4 * 1024 * 1024)
LISTEN_HOST = _env("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(_env("LISTEN_PORT", "9903"))
EXCLUDE_NON_PRESENT = _env_bool("EXCLUDE_NON_PRESENT", False)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**kwargs: str) -> str:
    parts = [f'{k}="{_escape(v)}"' for k, v in kwargs.items()]
    return "{" + ",".join(parts) + "}"


def parse_disks_ini(path: Path) -> Dict[str, Dict[str, str]]:
    disks: Dict[str, Dict[str, str]] = {}
    if not path.exists():
        return disks

    current: Optional[str] = None
    for raw_line in _read_text(path).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        section_match = SECTION_RE.match(line)
        if section_match:
            current = section_match.group(1)
            disks[current] = {}
            continue

        if current is None:
            continue

        kv_match = KEY_VALUE_RE.match(line)
        if kv_match:
            disks[current][kv_match.group(1)] = kv_match.group(2)

    return disks


def parse_smart_file(path: Path) -> Optional[SmartSnapshot]:
    attrs: Dict[int, SmartAttribute] = {}
    for raw_line in _read_text(path).splitlines():
        line = raw_line.rstrip("\n")
        attr_match = SMART_ATTR_RE.match(line)
        if not attr_match:
            continue

        tokens = line.split()
        if len(tokens) < 10:
            continue

        # smartctl row format tokens (simplified):
        # id name flag value worst thresh type updated when_failed raw...
        try:
            attr_id = int(tokens[0])
            name = tokens[1]
            value = int(tokens[3])
            worst = int(tokens[4])
            threshold = int(tokens[5])
        except ValueError:
            continue

        raw_text = " ".join(tokens[9:])
        raw_match = INT_RE.search(raw_text)
        raw_value = int(raw_match.group(0)) if raw_match else None

        attrs[attr_id] = SmartAttribute(
            attr_id=attr_id,
            name=name,
            value=value,
            worst=worst,
            threshold=threshold,
            raw=raw_value,
        )

    if not attrs:
        return None

    return SmartSnapshot(disk=path.name, mtime=path.stat().st_mtime, attrs=attrs)


def collect_snapshots(smart_dir: Path) -> List[SmartSnapshot]:
    if not smart_dir.exists() or not smart_dir.is_dir():
        return []

    snapshots: List[SmartSnapshot] = []
    for child in sorted(smart_dir.iterdir()):
        if not child.is_file():
            continue

        snapshot = parse_smart_file(child)
        if snapshot is not None:
            snapshots.append(snapshot)

    return snapshots


def _parse_float(value: str) -> Optional[float]:
    if not value or value == "*":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_int(value: str) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _is_non_present_disk(fields: Dict[str, str]) -> bool:
    status = fields.get("status", "")
    device = fields.get("device", "")
    return status.startswith("DISK_NP") or not device


def _device_disk_map(disks: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for disk, fields in disks.items():
        device = fields.get("device", "").strip().lower()
        if device:
            out[device] = disk
    return out


def _event_state_default() -> Dict[str, object]:
    return {
        "log_cursor": {"inode": 0, "offset": 0},
        "event_totals": {},
        "last_event_ts": {},
        "spin_state": {},
        "parse_errors_total": 0,
        "scan_errors_total": 0,
        "last_successful_log_scan_ts": 0.0,
    }


def _event_key(disk: str, device: str, event: str) -> str:
    return f"{disk}|{device}|{event}"


def _event_key_parts(key: str) -> Tuple[str, str, str]:
    parts = key.split("|", 2)
    if len(parts) != 3:
        return "unknown", "unknown", key
    return parts[0], parts[1], parts[2]


def _load_event_state(path: Path) -> Dict[str, object]:
    state = _event_state_default()
    if not path.exists():
        return state

    try:
        payload = json.loads(_read_text(path) or "{}")
    except json.JSONDecodeError:
        return state

    if isinstance(payload.get("log_cursor"), dict):
        state["log_cursor"] = {
            "inode": int(payload["log_cursor"].get("inode", 0) or 0),
            "offset": int(payload["log_cursor"].get("offset", 0) or 0),
        }

    for key in ("event_totals", "last_event_ts", "spin_state"):
        if isinstance(payload.get(key), dict):
            state[key] = payload[key]

    for key in ("parse_errors_total", "scan_errors_total"):
        try:
            state[key] = int(payload.get(key, 0) or 0)
        except (TypeError, ValueError):
            state[key] = 0

    try:
        state["last_successful_log_scan_ts"] = float(payload.get("last_successful_log_scan_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        state["last_successful_log_scan_ts"] = 0.0

    return state


def _save_event_state(path: Path, state: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def _parse_syslog_timestamp(month: str, day: str, hms: str, now: float) -> float:
    # Syslog timestamps omit year and timezone. Interpret in local time and
    # backshift year when near New Year's rollover.
    current_year = datetime.fromtimestamp(now).year
    dt = datetime.strptime(f"{month} {int(day)} {current_year} {hms}", "%b %d %Y %H:%M:%S")
    ts = dt.timestamp()
    if ts - now > 86400:
        dt = dt.replace(year=current_year - 1)
        ts = dt.timestamp()
    return ts


def _scan_syslog_events(
    path: Path,
    state: Dict[str, object],
    device_to_disk: Dict[str, str],
    now: float,
    initial_tail_bytes: int,
) -> Dict[str, object]:
    if not path.exists():
        state["last_successful_log_scan_ts"] = now
        return state

    stat_info = path.stat()
    cursor = state.get("log_cursor", {}) if isinstance(state.get("log_cursor"), dict) else {}
    inode = int(cursor.get("inode", 0) or 0)
    offset = int(cursor.get("offset", 0) or 0)

    rotated = inode and inode != stat_info.st_ino
    truncated = offset > stat_info.st_size
    first_scan = inode == 0 and offset == 0

    if rotated or truncated:
        offset = 0

    if first_scan and stat_info.st_size > initial_tail_bytes:
        offset = stat_info.st_size - initial_tail_bytes

    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        handle.seek(offset)
        chunk = handle.read()
        new_offset = handle.tell()

    event_totals = state.get("event_totals", {}) if isinstance(state.get("event_totals"), dict) else {}
    last_event_ts = state.get("last_event_ts", {}) if isinstance(state.get("last_event_ts"), dict) else {}
    spin_state = state.get("spin_state", {}) if isinstance(state.get("spin_state"), dict) else {}

    parse_errors_total = int(state.get("parse_errors_total", 0) or 0)

    for line in chunk.splitlines():
        match = SYSLOG_EVENT_RE.match(line)
        if not match:
            # Track lines that look close to target lifecycle events but fail parse.
            if "emhttpd:" in line and "/dev/sd" in line and (
                "spinning" in line.lower() or "smart" in line.lower()
            ):
                parse_errors_total += 1
            continue

        event = match.group("event")
        device = match.group("device").lower()
        disk = device_to_disk.get(device, f"unknown_{device}")
        event_ts = _parse_syslog_timestamp(match.group("month"), match.group("day"), match.group("hms"), now)

        key = _event_key(disk=disk, device=device, event=event)
        event_totals[key] = int(event_totals.get(key, 0) or 0) + 1
        prev = float(last_event_ts.get(key, 0.0) or 0.0)
        if event_ts > prev:
            last_event_ts[key] = event_ts

        if event in {"spinning up", "spinning down"}:
            next_state = "up" if event == "spinning up" else "down"
            prior = spin_state.get(disk, {}) if isinstance(spin_state.get(disk), dict) else {}
            prior_state = prior.get("state", "unknown")
            last_change = float(prior.get("last_change_ts", 0.0) or 0.0)
            if prior_state != next_state:
                last_change = event_ts
            spin_state[disk] = {
                "device": device,
                "state": next_state,
                "last_change_ts": last_change,
            }

    state["event_totals"] = event_totals
    state["last_event_ts"] = last_event_ts
    state["spin_state"] = spin_state
    state["parse_errors_total"] = parse_errors_total
    state["log_cursor"] = {"inode": int(stat_info.st_ino), "offset": int(new_offset)}
    state["last_successful_log_scan_ts"] = now
    return state


def render_metrics() -> str:
    start = time.perf_counter()
    now = time.time()

    disks = parse_disks_ini(DISKS_INI)
    snapshots = collect_snapshots(SMART_DIR)
    device_to_disk = _device_disk_map(disks)

    event_state = _load_event_state(STATE_PATH)
    try:
        event_state = _scan_syslog_events(SYSLOG_PATH, event_state, device_to_disk, now, SYSLOG_INITIAL_TAIL_BYTES)
    except Exception:
        event_state["scan_errors_total"] = int(event_state.get("scan_errors_total", 0) or 0) + 1
    try:
        _save_event_state(STATE_PATH, event_state)
    except OSError:
        # Keep serving metrics even if state cannot be persisted.
        pass

    lines: List[str] = []
    lines.append("# HELP unraid_smart_cache_scrape_timestamp_seconds Exporter scrape timestamp.")
    lines.append("# TYPE unraid_smart_cache_scrape_timestamp_seconds gauge")
    lines.append(f"unraid_smart_cache_scrape_timestamp_seconds {now:.3f}")

    lines.append("# HELP unraid_smart_cache_up Exporter health status (1=ok).")
    lines.append("# TYPE unraid_smart_cache_up gauge")
    lines.append("unraid_smart_cache_up 1")

    lines.append("# HELP unraid_smart_snapshot_mtime_seconds SMART snapshot file mtime.")
    lines.append("# TYPE unraid_smart_snapshot_mtime_seconds gauge")
    lines.append("# HELP unraid_smart_snapshot_age_seconds SMART snapshot age in seconds.")
    lines.append("# TYPE unraid_smart_snapshot_age_seconds gauge")

    lines.append("# HELP unraid_smart_attr_raw SMART attribute raw value.")
    lines.append("# TYPE unraid_smart_attr_raw gauge")

    lines.append("# HELP unraid_smart_attr_value SMART normalized value.")
    lines.append("# TYPE unraid_smart_attr_value gauge")

    lines.append("# HELP unraid_smart_attr_worst SMART worst normalized value.")
    lines.append("# TYPE unraid_smart_attr_worst gauge")

    lines.append("# HELP unraid_smart_attr_threshold SMART failure threshold value.")
    lines.append("# TYPE unraid_smart_attr_threshold gauge")

    lines.append("# HELP unraid_disk_info Disk metadata from disks.ini (constant 1).")
    lines.append("# TYPE unraid_disk_info gauge")

    lines.append("# HELP unraid_disk_reads_total Disk read counter from disks.ini.")
    lines.append("# TYPE unraid_disk_reads_total gauge")

    lines.append("# HELP unraid_disk_writes_total Disk write counter from disks.ini.")
    lines.append("# TYPE unraid_disk_writes_total gauge")

    lines.append("# HELP unraid_disk_errors_total Disk error counter from disks.ini.")
    lines.append("# TYPE unraid_disk_errors_total gauge")

    lines.append("# HELP unraid_disk_temp_celsius Disk temperature from disks.ini.")
    lines.append("# TYPE unraid_disk_temp_celsius gauge")

    lines.append("# HELP unraid_disk_spin_down_enabled Disk spin-down enabled flag from disks.ini.")
    lines.append("# TYPE unraid_disk_spin_down_enabled gauge")

    lines.append("# HELP unraid_disk_event_total Disk lifecycle events from emhttpd syslog.")
    lines.append("# TYPE unraid_disk_event_total counter")
    lines.append("# HELP unraid_disk_last_event_timestamp_seconds Last seen timestamp for disk lifecycle event.")
    lines.append("# TYPE unraid_disk_last_event_timestamp_seconds gauge")
    lines.append("# HELP unraid_disk_last_event_age_seconds Age of last seen disk lifecycle event.")
    lines.append("# TYPE unraid_disk_last_event_age_seconds gauge")

    lines.append("# HELP unraid_disk_last_spinup_timestamp_seconds Last seen spinning-up event timestamp.")
    lines.append("# TYPE unraid_disk_last_spinup_timestamp_seconds gauge")
    lines.append("# HELP unraid_disk_last_spindown_timestamp_seconds Last seen spinning-down event timestamp.")
    lines.append("# TYPE unraid_disk_last_spindown_timestamp_seconds gauge")
    lines.append("# HELP unraid_disk_last_smart_read_timestamp_seconds Last seen SMART-read event timestamp.")
    lines.append("# TYPE unraid_disk_last_smart_read_timestamp_seconds gauge")

    lines.append("# HELP unraid_disk_spin_state Disk spin state from lifecycle events (1=up, 0=down, -1=unknown).")
    lines.append("# TYPE unraid_disk_spin_state gauge")
    lines.append("# HELP unraid_disk_spin_state_last_change_timestamp_seconds Last spin state transition timestamp.")
    lines.append("# TYPE unraid_disk_spin_state_last_change_timestamp_seconds gauge")
    lines.append("# HELP unraid_disk_spin_state_age_seconds Age since last spin state transition.")
    lines.append("# TYPE unraid_disk_spin_state_age_seconds gauge")

    lines.append("# HELP unraid_exporter_log_parse_errors_total Lifecycle log lines that could not be parsed.")
    lines.append("# TYPE unraid_exporter_log_parse_errors_total counter")
    lines.append("# HELP unraid_exporter_log_scan_errors_total Syslog scan failures.")
    lines.append("# TYPE unraid_exporter_log_scan_errors_total counter")
    lines.append("# HELP unraid_exporter_last_successful_log_scan_timestamp_seconds Last successful syslog scan timestamp.")
    lines.append("# TYPE unraid_exporter_last_successful_log_scan_timestamp_seconds gauge")
    lines.append("# HELP unraid_exporter_log_cursor_offset_bytes Current syslog byte offset cursor.")
    lines.append("# TYPE unraid_exporter_log_cursor_offset_bytes gauge")
    lines.append("# HELP unraid_exporter_log_lag_seconds Age since newest observed lifecycle event.")
    lines.append("# TYPE unraid_exporter_log_lag_seconds gauge")

    lines.append("# HELP unraid_smart_cache_scrape_duration_seconds Exporter scrape render duration.")
    lines.append("# TYPE unraid_smart_cache_scrape_duration_seconds gauge")

    # Convenience SMART metrics for common attributes.
    lines.append("# HELP unraid_smart_start_stop_count SMART Start_Stop_Count raw value (attr 4).")
    lines.append("# TYPE unraid_smart_start_stop_count gauge")
    lines.append("# HELP unraid_smart_reallocated_sectors SMART Reallocated_Sector_Ct raw value (attr 5).")
    lines.append("# TYPE unraid_smart_reallocated_sectors gauge")
    lines.append("# HELP unraid_smart_power_on_hours SMART Power_On_Hours raw value (attr 9).")
    lines.append("# TYPE unraid_smart_power_on_hours gauge")
    lines.append("# HELP unraid_smart_temperature_celsius SMART Temperature_Celsius raw value (attr 194).")
    lines.append("# TYPE unraid_smart_temperature_celsius gauge")
    lines.append("# HELP unraid_smart_pending_sectors SMART Current_Pending_Sector raw value (attr 197).")
    lines.append("# TYPE unraid_smart_pending_sectors gauge")
    lines.append("# HELP unraid_smart_offline_uncorrectable SMART Offline_Uncorrectable raw value (attr 198).")
    lines.append("# TYPE unraid_smart_offline_uncorrectable gauge")
    lines.append("# HELP unraid_smart_udma_crc_errors SMART UDMA_CRC_Error_Count raw value (attr 199).")
    lines.append("# TYPE unraid_smart_udma_crc_errors gauge")

    convenience = {
        4: "unraid_smart_start_stop_count",
        5: "unraid_smart_reallocated_sectors",
        9: "unraid_smart_power_on_hours",
        194: "unraid_smart_temperature_celsius",
        197: "unraid_smart_pending_sectors",
        198: "unraid_smart_offline_uncorrectable",
        199: "unraid_smart_udma_crc_errors",
    }

    for snapshot in snapshots:
        disk = snapshot.disk
        lines.append(f'unraid_smart_snapshot_mtime_seconds{_labels(disk=disk)} {snapshot.mtime:.0f}')
        lines.append(f'unraid_smart_snapshot_age_seconds{_labels(disk=disk)} {max(0.0, now - snapshot.mtime):.3f}')

        for attr in snapshot.attrs.values():
            base_labels = _labels(disk=disk, attr_id=str(attr.attr_id), attr_name=attr.name)
            lines.append(f"unraid_smart_attr_value{base_labels} {attr.value}")
            lines.append(f"unraid_smart_attr_worst{base_labels} {attr.worst}")
            lines.append(f"unraid_smart_attr_threshold{base_labels} {attr.threshold}")
            if attr.raw is not None:
                lines.append(f"unraid_smart_attr_raw{base_labels} {attr.raw}")

            metric = convenience.get(attr.attr_id)
            if metric and attr.raw is not None:
                lines.append(f'{metric}{_labels(disk=disk)} {attr.raw}')

    for disk, fields in sorted(disks.items()):
        if EXCLUDE_NON_PRESENT and _is_non_present_disk(fields):
            continue

        labels = _labels(
            disk=disk,
            device=fields.get("device", ""),
            status=fields.get("status", ""),
            disk_type=fields.get("type", ""),
            transport=fields.get("transport", ""),
        )
        lines.append(f"unraid_disk_info{labels} 1")

        reads = _parse_int(fields.get("numReads", ""))
        if reads is not None:
            lines.append(f'unraid_disk_reads_total{_labels(disk=disk)} {reads}')

        writes = _parse_int(fields.get("numWrites", ""))
        if writes is not None:
            lines.append(f'unraid_disk_writes_total{_labels(disk=disk)} {writes}')

        errors = _parse_int(fields.get("numErrors", ""))
        if errors is not None:
            lines.append(f'unraid_disk_errors_total{_labels(disk=disk)} {errors}')

        temp = _parse_float(fields.get("temp", ""))
        if temp is not None:
            lines.append(f'unraid_disk_temp_celsius{_labels(disk=disk)} {temp}')

        spin = _parse_int(fields.get("spundown", ""))
        if spin is not None:
            lines.append(f'unraid_disk_spin_down_enabled{_labels(disk=disk)} {spin}')

    # Event-derived metrics.
    event_totals = event_state.get("event_totals", {}) if isinstance(event_state.get("event_totals"), dict) else {}
    last_event_ts = event_state.get("last_event_ts", {}) if isinstance(event_state.get("last_event_ts"), dict) else {}
    spin_state = event_state.get("spin_state", {}) if isinstance(event_state.get("spin_state"), dict) else {}

    last_by_event: Dict[Tuple[str, str], float] = {}
    newest_event_ts = 0.0

    for key, value in sorted(event_totals.items()):
        disk, device, event = _event_key_parts(key)
        count = int(value or 0)
        lines.append(f'unraid_disk_event_total{_labels(disk=disk, device=device, event=event)} {count}')

    for key, value in sorted(last_event_ts.items()):
        disk, device, event = _event_key_parts(key)
        ts = float(value or 0.0)
        lines.append(f'unraid_disk_last_event_timestamp_seconds{_labels(disk=disk, device=device, event=event)} {ts:.0f}')
        lines.append(
            f'unraid_disk_last_event_age_seconds{_labels(disk=disk, device=device, event=event)} '
            f'{max(0.0, now - ts):.3f}'
        )
        last_by_event[(disk, event)] = max(last_by_event.get((disk, event), 0.0), ts)
        newest_event_ts = max(newest_event_ts, ts)

    for disk_entry, fields in sorted(spin_state.items()):
        if not isinstance(fields, dict):
            continue
        device = str(fields.get("device", ""))
        state_name = str(fields.get("state", "unknown"))
        state_value = 1 if state_name == "up" else 0 if state_name == "down" else -1
        last_change = float(fields.get("last_change_ts", 0.0) or 0.0)

        labels = _labels(disk=disk_entry, device=device)
        lines.append(f"unraid_disk_spin_state{labels} {state_value}")
        lines.append(f"unraid_disk_spin_state_last_change_timestamp_seconds{labels} {last_change:.0f}")
        lines.append(f"unraid_disk_spin_state_age_seconds{labels} {max(0.0, now - last_change):.3f}")

    for (disk, event), metric_name in (
        ((None, "spinning up"), "unraid_disk_last_spinup_timestamp_seconds"),
        ((None, "spinning down"), "unraid_disk_last_spindown_timestamp_seconds"),
        ((None, "read SMART"), "unraid_disk_last_smart_read_timestamp_seconds"),
    ):
        del disk  # only the event token is used in this loop shape.
        event_token = event
        for disk_name in sorted({k[0] for k in last_by_event.keys()}):
            ts = last_by_event.get((disk_name, event_token), 0.0)
            if ts > 0:
                lines.append(f'{metric_name}{_labels(disk=disk_name)} {ts:.0f}')

    parse_errors = int(event_state.get("parse_errors_total", 0) or 0)
    scan_errors = int(event_state.get("scan_errors_total", 0) or 0)
    last_scan = float(event_state.get("last_successful_log_scan_ts", 0.0) or 0.0)
    cursor = event_state.get("log_cursor", {}) if isinstance(event_state.get("log_cursor"), dict) else {}
    cursor_offset = int(cursor.get("offset", 0) or 0)

    lines.append(f"unraid_exporter_log_parse_errors_total {parse_errors}")
    lines.append(f"unraid_exporter_log_scan_errors_total {scan_errors}")
    lines.append(f"unraid_exporter_last_successful_log_scan_timestamp_seconds {last_scan:.0f}")
    lines.append(f"unraid_exporter_log_cursor_offset_bytes {cursor_offset}")
    if newest_event_ts > 0:
        lines.append(f"unraid_exporter_log_lag_seconds {max(0.0, now - newest_event_ts):.3f}")
    else:
        lines.append("unraid_exporter_log_lag_seconds -1")

    lines.append(f"unraid_smart_cache_scrape_duration_seconds {time.perf_counter() - start:.6f}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unraid SMART cache Prometheus exporter")
    parser.add_argument(
        "--smart-dir",
        default=str(SMART_DIR),
        help="Path to Unraid SMART cache directory (default from SMART_DIR env).",
    )
    parser.add_argument(
        "--disks-ini",
        default=str(DISKS_INI),
        help="Path to Unraid disks.ini file (default from DISKS_INI env).",
    )
    parser.add_argument(
        "--syslog-path",
        default=str(SYSLOG_PATH),
        help="Path to Unraid syslog file for disk lifecycle event parsing (default from SYSLOG_PATH env).",
    )
    parser.add_argument(
        "--state-path",
        default=str(STATE_PATH),
        help="Path to exporter state file for log cursor + counters (default from STATE_PATH env).",
    )
    parser.add_argument(
        "--syslog-initial-tail-bytes",
        type=int,
        default=SYSLOG_INITIAL_TAIL_BYTES,
        help=(
            "Bytes to tail on first syslog scan when no cursor exists "
            "(default from SYSLOG_INITIAL_TAIL_BYTES env)."
        ),
    )
    parser.add_argument(
        "--listen-host",
        default=LISTEN_HOST,
        help="Host interface to bind (default from LISTEN_HOST env).",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=LISTEN_PORT,
        help="TCP port to listen on (default from LISTEN_PORT env).",
    )
    parser.add_argument(
        "--exclude-non-present",
        action=argparse.BooleanOptionalAction,
        default=EXCLUDE_NON_PRESENT,
        help=(
            "Exclude non-present disk slots from disks.ini metrics "
            "(default from EXCLUDE_NON_PRESENT env)."
        ),
    )
    return parser.parse_args()


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return

        if self.path != "/metrics":
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found\n")
            return

        try:
            payload = render_metrics().encode("utf-8")
        except Exception as exc:  # pragma: no cover
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"error: {exc}\n".encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    global SMART_DIR, DISKS_INI, SYSLOG_PATH, STATE_PATH, SYSLOG_INITIAL_TAIL_BYTES
    global LISTEN_HOST, LISTEN_PORT, EXCLUDE_NON_PRESENT

    args = parse_args()
    SMART_DIR = Path(args.smart_dir)
    DISKS_INI = Path(args.disks_ini)
    SYSLOG_PATH = Path(args.syslog_path)
    STATE_PATH = Path(args.state_path)
    SYSLOG_INITIAL_TAIL_BYTES = args.syslog_initial_tail_bytes
    LISTEN_HOST = args.listen_host
    LISTEN_PORT = args.listen_port
    EXCLUDE_NON_PRESENT = args.exclude_non_present

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), MetricsHandler)
    print(f"listening on {LISTEN_HOST}:{LISTEN_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
