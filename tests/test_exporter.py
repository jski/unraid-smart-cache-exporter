import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import exporter


class ExporterTests(unittest.TestCase):
    def test_detect_syslog_timezone_name_from_tz_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tz_file = Path(tmp) / "TZ"
            tz_file.write_text("America/New_York\n", encoding="utf-8")

            detected = exporter._detect_syslog_timezone_name(
                tz_file_paths=[tz_file],
                localtime_paths=[],
            )

        self.assertEqual(detected, "America/New_York")

    def test_detect_syslog_timezone_name_from_key_value_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tz_file = Path(tmp) / "clock"
            tz_file.write_text('ZONE="America/Chicago"\n', encoding="utf-8")

            detected = exporter._detect_syslog_timezone_name(
                tz_file_paths=[tz_file],
                localtime_paths=[],
            )

        self.assertEqual(detected, "America/Chicago")

    def test_parse_syslog_timestamp_honors_timezone(self):
        # Simulate parse at 2026-03-05 16:35:00 UTC.
        now = datetime(2026, 3, 5, 16, 35, 0, tzinfo=timezone.utc).timestamp()
        est = timezone(timedelta(hours=-5))

        ts = exporter._parse_syslog_timestamp("Mar", "5", "11:30:52", now, est)
        expected = datetime(2026, 3, 5, 11, 30, 52, tzinfo=est).timestamp()

        self.assertEqual(int(ts), int(expected))

    def test_parse_disks_ini(self):
        disks = exporter.parse_disks_ini(Path("tests/fixtures/disks.ini"))
        self.assertIn("disk1", disks)
        self.assertIn("disk10", disks)
        self.assertEqual(disks["disk1"]["device"], "sdf")
        self.assertEqual(disks["disk2"]["numErrors"], "1")

    def test_parse_smart_file(self):
        snap = exporter.parse_smart_file(Path("tests/fixtures/smart/disk1"))
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap.disk, "disk1")
        self.assertIn(194, snap.attrs)
        self.assertEqual(snap.attrs[194].raw, 38)
        self.assertEqual(snap.attrs[199].raw, 1)

    def test_render_metrics(self):
        original_smart = exporter.SMART_DIR
        original_disks = exporter.DISKS_INI
        original_exclude = exporter.EXCLUDE_NON_PRESENT
        original_syslog = exporter.SYSLOG_PATH
        original_state = exporter.STATE_PATH
        original_tail = exporter.SYSLOG_INITIAL_TAIL_BYTES
        try:
            with tempfile.TemporaryDirectory() as tmp:
                exporter.SMART_DIR = Path("tests/fixtures/smart")
                exporter.DISKS_INI = Path("tests/fixtures/disks.ini")
                exporter.EXCLUDE_NON_PRESENT = False
                exporter.SYSLOG_PATH = Path("tests/fixtures/syslog.log")
                exporter.STATE_PATH = Path(tmp) / "state.json"
                exporter.SYSLOG_INITIAL_TAIL_BYTES = 1024 * 1024
                metrics = exporter.render_metrics()
        finally:
            exporter.SMART_DIR = original_smart
            exporter.DISKS_INI = original_disks
            exporter.EXCLUDE_NON_PRESENT = original_exclude
            exporter.SYSLOG_PATH = original_syslog
            exporter.STATE_PATH = original_state
            exporter.SYSLOG_INITIAL_TAIL_BYTES = original_tail

        self.assertIn("unraid_smart_attr_raw", metrics)
        self.assertIn('unraid_disk_info{disk="disk1"', metrics)
        self.assertIn('unraid_disk_info{disk="disk10"', metrics)
        self.assertIn('unraid_smart_temperature_celsius{disk="disk1"} 38', metrics)
        self.assertIn("unraid_smart_cache_scrape_duration_seconds", metrics)

        # Event-derived metrics should be present.
        self.assertIn(
            'unraid_disk_event_total{disk="unknown_sde",device="sde",event="spinning down",event_source="explicit"} 1',
            metrics,
        )
        self.assertIn(
            'unraid_disk_event_total{disk="unknown_sde",device="sde",event="read SMART",event_source="explicit"} 1',
            metrics,
        )
        self.assertIn(
            'unraid_disk_event_total{disk="disk1",device="sdf",event="spinning up",event_source="explicit"} 1',
            metrics,
        )
        self.assertIn(
            'unraid_disk_event_total{disk="disk1",device="sdf",event="spinning down",event_source="explicit"} 1',
            metrics,
        )
        self.assertIn(
            'unraid_disk_spin_state{disk="disk1",device="sdf",state_source="explicit",confidence="high"} 0',
            metrics,
        )
        self.assertIn('unraid_disk_last_spinup_timestamp_seconds{disk="disk1"}', metrics)
        self.assertIn('unraid_disk_last_spindown_timestamp_seconds{disk="disk1"}', metrics)
        self.assertIn("unraid_exporter_log_parse_errors_total 1", metrics)
        self.assertIn("unraid_exporter_state_persist_ok 1", metrics)

    def test_render_metrics_reports_state_persist_failure(self):
        original_smart = exporter.SMART_DIR
        original_disks = exporter.DISKS_INI
        original_exclude = exporter.EXCLUDE_NON_PRESENT
        original_syslog = exporter.SYSLOG_PATH
        original_state = exporter.STATE_PATH
        original_tail = exporter.SYSLOG_INITIAL_TAIL_BYTES
        original_errors = exporter.STATE_PERSIST_ERRORS_TOTAL
        original_last_error = exporter.LAST_STATE_PERSIST_ERROR_TS
        try:
            with tempfile.TemporaryDirectory() as tmp:
                exporter.SMART_DIR = Path("tests/fixtures/smart")
                exporter.DISKS_INI = Path("tests/fixtures/disks.ini")
                exporter.EXCLUDE_NON_PRESENT = False
                exporter.SYSLOG_PATH = Path("tests/fixtures/syslog.log")
                exporter.STATE_PATH = Path(tmp) / "state.json"
                exporter.SYSLOG_INITIAL_TAIL_BYTES = 1024 * 1024
                exporter.STATE_PERSIST_ERRORS_TOTAL = 0
                exporter.LAST_STATE_PERSIST_ERROR_TS = 0.0
                with mock.patch("exporter._save_event_state", side_effect=OSError("write failed")):
                    metrics = exporter.render_metrics()
        finally:
            exporter.SMART_DIR = original_smart
            exporter.DISKS_INI = original_disks
            exporter.EXCLUDE_NON_PRESENT = original_exclude
            exporter.SYSLOG_PATH = original_syslog
            exporter.STATE_PATH = original_state
            exporter.SYSLOG_INITIAL_TAIL_BYTES = original_tail
            exporter.STATE_PERSIST_ERRORS_TOTAL = original_errors
            exporter.LAST_STATE_PERSIST_ERROR_TS = original_last_error

        self.assertIn("unraid_exporter_state_persist_ok 0", metrics)
        self.assertIn("unraid_exporter_state_persist_errors_total 1", metrics)

    def test_render_metrics_excludes_non_present_disks(self):
        original_smart = exporter.SMART_DIR
        original_disks = exporter.DISKS_INI
        original_exclude = exporter.EXCLUDE_NON_PRESENT
        original_syslog = exporter.SYSLOG_PATH
        original_state = exporter.STATE_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                exporter.SMART_DIR = Path("tests/fixtures/smart")
                exporter.DISKS_INI = Path("tests/fixtures/disks.ini")
                exporter.EXCLUDE_NON_PRESENT = True
                exporter.SYSLOG_PATH = Path("tests/fixtures/syslog.log")
                exporter.STATE_PATH = Path(tmp) / "state.json"
                metrics = exporter.render_metrics()
        finally:
            exporter.SMART_DIR = original_smart
            exporter.DISKS_INI = original_disks
            exporter.EXCLUDE_NON_PRESENT = original_exclude
            exporter.SYSLOG_PATH = original_syslog
            exporter.STATE_PATH = original_state

        self.assertIn('unraid_disk_info{disk="disk1"', metrics)
        self.assertNotIn('unraid_disk_info{disk="disk10"', metrics)

    def test_event_cursor_prevents_double_counting(self):
        original_smart = exporter.SMART_DIR
        original_disks = exporter.DISKS_INI
        original_exclude = exporter.EXCLUDE_NON_PRESENT
        original_syslog = exporter.SYSLOG_PATH
        original_state = exporter.STATE_PATH
        original_tail = exporter.SYSLOG_INITIAL_TAIL_BYTES
        try:
            with tempfile.TemporaryDirectory() as tmp:
                exporter.SMART_DIR = Path("tests/fixtures/smart")
                exporter.DISKS_INI = Path("tests/fixtures/disks.ini")
                exporter.EXCLUDE_NON_PRESENT = False
                exporter.SYSLOG_PATH = Path("tests/fixtures/syslog.log")
                exporter.STATE_PATH = Path(tmp) / "state.json"
                exporter.SYSLOG_INITIAL_TAIL_BYTES = 1024 * 1024

                first = exporter.render_metrics()
                second = exporter.render_metrics()
        finally:
            exporter.SMART_DIR = original_smart
            exporter.DISKS_INI = original_disks
            exporter.EXCLUDE_NON_PRESENT = original_exclude
            exporter.SYSLOG_PATH = original_syslog
            exporter.STATE_PATH = original_state
            exporter.SYSLOG_INITIAL_TAIL_BYTES = original_tail

        needle = 'unraid_disk_event_total{disk="disk1",device="sdf",event="spinning down",event_source="explicit"} 1'
        self.assertIn(needle, first)
        self.assertIn(needle, second)

    def test_scan_syslog_read_smart_marks_up_when_prior_down(self):
        state = exporter._event_state_default()
        state["spin_state"] = {
            "disk3": {
                "device": "sde",
                "state": "down",
                "last_change_ts": 100.0,
                "state_source": "explicit",
                "confidence": "high",
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            syslog = Path(tmp) / "syslog.log"
            syslog.write_text("Mar  5 03:49:34 unraid emhttpd: read SMART /dev/sde\n", encoding="utf-8")
            out = exporter._scan_syslog_events(
                syslog,
                state,
                {"sde": "disk3"},
                datetime(2026, 3, 5, 9, 0, 0, tzinfo=timezone.utc).timestamp(),
                1024 * 1024,
            )

        spin = out.get("spin_state", {})
        assert isinstance(spin, dict)
        disk3 = spin.get("disk3", {})
        assert isinstance(disk3, dict)
        self.assertEqual(disk3.get("state"), "up")
        self.assertEqual(disk3.get("state_source"), "read_smart")
        self.assertEqual(disk3.get("confidence"), "medium")

        events = out.get("event_totals", {})
        assert isinstance(events, dict)
        self.assertEqual(events.get("disk3|sde|read SMART|explicit"), 1)

    def test_infer_spinup_from_counter_delta_when_state_down(self):
        state = exporter._event_state_default()
        state["spin_state"] = {
            "disk1": {
                "device": "sdf",
                "state": "down",
                "last_change_ts": 100.0,
                "state_source": "explicit",
                "confidence": "high",
            }
        }
        state["disk_counters"] = {
            "disk1": {"reads": 100, "writes": 200, "errors": 0}
        }

        disks = {
            "disk1": {
                "device": "sdf",
                "status": "DISK_OK",
                "numReads": "101",
                "numWrites": "200",
                "numErrors": "0",
            }
        }

        out = exporter._infer_spinup_from_disk_counters(state, disks, 1234.0)
        events = out.get("event_totals", {})
        assert isinstance(events, dict)
        self.assertEqual(events.get("disk1|sdf|spinning up|inferred"), 1)

        spin = out.get("spin_state", {})
        assert isinstance(spin, dict)
        disk1 = spin.get("disk1", {})
        assert isinstance(disk1, dict)
        self.assertEqual(disk1.get("state"), "up")
        self.assertEqual(disk1.get("state_source"), "inferred")
        self.assertEqual(disk1.get("confidence"), "medium")
        self.assertEqual(int(out.get("inferred_transitions_total", 0)), 1)


if __name__ == "__main__":
    unittest.main()
