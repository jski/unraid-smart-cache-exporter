#!/usr/bin/env python3
"""Unraid SMART cache exporter.

Reads Unraid-managed SMART cache files from /var/local/emhttp/smart and disk
state from /var/local/emhttp/disks.ini, then exposes Prometheus metrics.

This exporter does not run smartctl directly.
"""

from __future__ import annotations

import os
import argparse
import re
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional

SMART_ATTR_RE = re.compile(r"^\s*(\d+)\s+([A-Za-z0-9_\-]+)\s+")
SECTION_RE = re.compile(r'^\["?([^"\]]+)"?\]$')
KEY_VALUE_RE = re.compile(r'^(\w+)="(.*)"$')
INT_RE = re.compile(r"-?\d+")


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


SMART_DIR = Path(_env("SMART_DIR", "/var/local/emhttp/smart"))
DISKS_INI = Path(_env("DISKS_INI", "/var/local/emhttp/disks.ini"))
LISTEN_HOST = _env("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(_env("LISTEN_PORT", "9903"))


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


def render_metrics() -> str:
    start = time.perf_counter()
    now = time.time()
    disks = parse_disks_ini(DISKS_INI)
    snapshots = collect_snapshots(SMART_DIR)

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
    global SMART_DIR, DISKS_INI, LISTEN_HOST, LISTEN_PORT
    args = parse_args()
    SMART_DIR = Path(args.smart_dir)
    DISKS_INI = Path(args.disks_ini)
    LISTEN_HOST = args.listen_host
    LISTEN_PORT = args.listen_port
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), MetricsHandler)
    print(f"listening on {LISTEN_HOST}:{LISTEN_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
