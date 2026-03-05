import tempfile
import unittest
from pathlib import Path

import exporter


class ExporterTests(unittest.TestCase):
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
        self.assertIn('unraid_disk_event_total{disk="unknown_sde",device="sde",event="spinning down"} 1', metrics)
        self.assertIn('unraid_disk_event_total{disk="unknown_sde",device="sde",event="read SMART"} 1', metrics)
        self.assertIn('unraid_disk_event_total{disk="disk1",device="sdf",event="spinning up"} 1', metrics)
        self.assertIn('unraid_disk_event_total{disk="disk1",device="sdf",event="spinning down"} 1', metrics)
        self.assertIn('unraid_disk_spin_state{disk="disk1",device="sdf"} 0', metrics)
        self.assertIn('unraid_disk_last_spinup_timestamp_seconds{disk="disk1"}', metrics)
        self.assertIn('unraid_disk_last_spindown_timestamp_seconds{disk="disk1"}', metrics)
        self.assertIn("unraid_exporter_log_parse_errors_total 1", metrics)

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

        needle = 'unraid_disk_event_total{disk="disk1",device="sdf",event="spinning down"} 1'
        self.assertIn(needle, first)
        self.assertIn(needle, second)


if __name__ == "__main__":
    unittest.main()
