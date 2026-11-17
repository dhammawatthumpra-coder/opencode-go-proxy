"""python -m unittest discover -s test

Only the pure helpers are covered; anything that needs a tray icon or a live
proxy is left to a manual run (see the README).
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tray_app
except Exception:  # pystray / pillow missing
    tray_app = None


@unittest.skipIf(tray_app is None, "tray dependencies not installed")
class Helpers(unittest.TestCase):
    NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)

    def test_resets_in(self):
        f = tray_app.resets_in
        self.assertEqual(f("2026-11-17T14:53:19.000Z", self.NOW), "Resets in 2h 53m")
        self.assertEqual(f("2026-11-22T09:00:00.000Z", self.NOW), "Resets in 4d 21h")
        self.assertEqual(f("2026-11-17T11:00:00.000Z", self.NOW), "resetting")
        self.assertEqual(f("", self.NOW), "")
        self.assertEqual(f("not a date", self.NOW), "")

    def test_clamp_alpha(self):
        f = tray_app.clamp_alpha
        self.assertEqual(f(0.5), 0.5)
        self.assertEqual(f(2), 1.0)
        self.assertEqual(f(0.1), 0.3)
        self.assertEqual(f("nonsense"), 1.0)

    def test_short_tokens(self):
        f = tray_app.short_tokens
        self.assertEqual(f(999), "999")
        self.assertEqual(f(1_500_000), "1.5M")
        self.assertEqual(f(0), "0")


if __name__ == "__main__":
    unittest.main()
