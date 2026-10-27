"""The frequency plan chart is ONE piece of code shown in two places: the site page
(app/static/fpchart.*) and the hub's Control Center (hub/app/static/cc/fpchart.*).
They are copied, not shared (separate images), so this keeps the copies identical.

Run from the repo root:  python3 tests/test_fpchart_same.py
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Same(unittest.TestCase):
    def test_site_and_hub_copies_match(self):
        for f in ("fpchart.js", "fpchart.css"):
            with open(os.path.join(ROOT, "app", "static", f), "rb") as a, open(os.path.join(ROOT, "hub", "app", "static", "cc", f), "rb") as b:
                self.assertEqual(a.read(), b.read(), f"{f} differs — copy app/static/{f} to hub/app/static/cc/{f}")


if __name__ == "__main__":
    unittest.main()
