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
        try:
            exporter.SMART_DIR = Path("tests/fixtures/smart")
            exporter.DISKS_INI = Path("tests/fixtures/disks.ini")
            exporter.EXCLUDE_NON_PRESENT = False
            metrics = exporter.render_metrics()
        finally:
            exporter.SMART_DIR = original_smart
            exporter.DISKS_INI = original_disks
            exporter.EXCLUDE_NON_PRESENT = original_exclude

        self.assertIn("unraid_smart_attr_raw", metrics)
        self.assertIn('unraid_disk_info{disk="disk1"', metrics)
        self.assertIn('unraid_disk_info{disk="disk10"', metrics)
        self.assertIn('unraid_smart_temperature_celsius{disk="disk1"} 38', metrics)
        self.assertIn("unraid_smart_cache_scrape_duration_seconds", metrics)

    def test_render_metrics_excludes_non_present_disks(self):
        original_smart = exporter.SMART_DIR
        original_disks = exporter.DISKS_INI
        original_exclude = exporter.EXCLUDE_NON_PRESENT
        try:
            exporter.SMART_DIR = Path("tests/fixtures/smart")
            exporter.DISKS_INI = Path("tests/fixtures/disks.ini")
            exporter.EXCLUDE_NON_PRESENT = True
            metrics = exporter.render_metrics()
        finally:
            exporter.SMART_DIR = original_smart
            exporter.DISKS_INI = original_disks
            exporter.EXCLUDE_NON_PRESENT = original_exclude

        self.assertIn('unraid_disk_info{disk="disk1"', metrics)
        self.assertNotIn('unraid_disk_info{disk="disk10"', metrics)


if __name__ == "__main__":
    unittest.main()
