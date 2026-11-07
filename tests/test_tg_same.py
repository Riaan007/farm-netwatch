"""The Telegram sender and outbox are ONE piece of code used by the site and the
hub (app/tg*.py and hub/app/tg*.py). They are copied, not shared (separate
images, and hub tests mount hub/app alone), so this keeps the copies identical.

Run from the repo root:  python3 tests/test_tg_same.py
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Same(unittest.TestCase):
    def test_site_and_hub_copies_match(self):
        for f in ("tgapi.py", "tgoutbox.py", "tgbotcore.py"):
            with open(os.path.join(ROOT, "app", f), "rb") as a, \
                    open(os.path.join(ROOT, "hub", "app", f), "rb") as b:
                self.assertEqual(a.read(), b.read(), f"{f} differs — copy app/{f} to hub/app/{f}")


if __name__ == "__main__":
    unittest.main()
