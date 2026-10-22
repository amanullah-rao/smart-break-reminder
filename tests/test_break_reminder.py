import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import Break_Reminder as br  # noqa: E402


class ValidMinutesTest(unittest.TestCase):
    def test_accepts_range(self):
        self.assertEqual(br.valid_minutes("25"), 25)
        self.assertEqual(br.valid_minutes(0.05), 0.05)
        self.assertEqual(br.valid_minutes(1440), 1440)

    def test_rejects_bad_values(self):
        for value in ("0", "0.01", "1441", "-5", "nan", "inf", "abc", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                br.valid_minutes(value)


class FmtTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(br.fmt(0), "00:00")
        self.assertEqual(br.fmt(59.2), "01:00")   # rounds up so 00:00 means done
        self.assertEqual(br.fmt(3600), "60:00")
        self.assertEqual(br.fmt(-3), "00:00")


class FileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "data.json"

    def test_write_json_round_trip(self):
        br.write_json(self.path, {"a": 1})
        self.assertEqual(br.read_json(self.path, None), {"a": 1})
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_read_json_default_on_garbage(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(br.read_json(self.path, []), [])

    def test_load_settings_validates(self):
        self.path.write_text(json.dumps({"work_minutes": 50, "break_minutes": "nan", "repeat": "yes",
                                         "sound": "beep", "close_action": "bogus", "close_to_tray": True}),
                             encoding="utf-8")
        with mock.patch.object(br, "CONFIG", self.path):
            settings = br.load_settings()
        self.assertEqual(settings["work_minutes"], 50)
        self.assertEqual(settings["break_minutes"], br.DEFAULTS["break_minutes"])
        self.assertIs(settings["repeat"], True)
        self.assertEqual(settings["sound"], "beep")
        self.assertEqual(settings["close_action"], "tray")

    def test_load_history_migrates_and_filters(self):
        self.path.write_text(json.dumps([
            {"timestamp": "2026-10-22T09:00:00", "event": "completed"},
            "21-10-2026 18:30 - Break skipped",
            {"timestamp": "bad", "event": "completed"},
            {"timestamp": "2026-10-20T09:00:00", "event": "unknown"},
            42,
        ]), encoding="utf-8")
        with mock.patch.object(br, "HISTORY", self.path):
            history = br.load_history()
        self.assertEqual(history, [
            {"timestamp": "2026-10-22T09:00:00", "event": "completed"},
            {"timestamp": "2026-10-21T18:30:00", "event": "skipped"},
        ])

    def test_load_history_is_trimmed(self):
        entries = [{"timestamp": "2026-10-22T09:00:00", "event": "completed"}] * (br.HISTORY_LIMIT + 50)
        self.path.write_text(json.dumps(entries), encoding="utf-8")
        with mock.patch.object(br, "HISTORY", self.path):
            self.assertEqual(len(br.load_history()), br.HISTORY_LIMIT)


class ThemeTest(unittest.TestCase):
    def test_resolve_theme(self):
        self.assertEqual(br.resolve_theme("light", True), "light")
        self.assertEqual(br.resolve_theme("dark", False), "dark")
        self.assertEqual(br.resolve_theme("system", True), "dark")
        self.assertEqual(br.resolve_theme("system", False), "light")

    def test_load_settings_theme(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            with mock.patch.object(br, "CONFIG", path):
                self.assertEqual(br.load_settings()["theme"], "system")   # default
                path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
                self.assertEqual(br.load_settings()["theme"], "dark")
                path.write_text(json.dumps({"theme": "purple"}), encoding="utf-8")
                self.assertEqual(br.load_settings()["theme"], "system")

    def test_themes_have_same_keys(self):
        self.assertEqual(set(br.THEMES["light"]), set(br.THEMES["dark"]))


class CompletedTodayTest(unittest.TestCase):
    def test_counts_only_completed_today(self):
        history = [
            {"timestamp": "2026-10-22T10:00:00", "event": "completed"},
            {"timestamp": "2026-10-22T09:00:00", "event": "skipped"},
            {"timestamp": "2026-10-22T08:00:00", "event": "completed"},
            {"timestamp": "2026-10-21T08:00:00", "event": "completed"},
        ]
        self.assertEqual(br.completed_today(history, "2026-10-22"), 2)
        self.assertEqual(br.completed_today([], "2026-10-22"), 0)


if __name__ == "__main__":
    unittest.main()
