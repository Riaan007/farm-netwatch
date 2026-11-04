"""Device categories are one list (app/identify.py CATEGORIES) that several
places must all be able to show: the site page (app/static/index.html), the
hub's Control Center (hub/app/static/cc/core.js) and classic site page
(hub/app/static/site.html), and the network diagram (app/topology.py). They are
separate files in separate images, so this keeps them in step: a category the
scanner can produce is one every page can draw.

test_device_meta.py checks the site page's labels and edit menu, Kuma's tags
and the asset groups from inside the site image; this checks the rest, from the
source files alone.

Run from the repo root:  python3 tests/test_categories_same.py
"""
import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def py_value(path, name):
    """The literal assigned to `name` at the top of a Python file (no import)."""
    for node in ast.parse(read(*path)).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            v = node.value
            if isinstance(v, ast.Call):          # frozenset({...})
                v = v.args[0]
            return ast.literal_eval(v)
    raise AssertionError(f"{name} not found in {'/'.join(path)}")


def js_keys(text, start):
    """Top-level keys of the object literal that follows `start` in `text`."""
    i = text.index(start)
    i = text.index("{", i)
    depth, j = 0, i
    while True:
        c = text[j]
        depth += c == "{"
        depth -= c == "}"
        if depth == 0:
            break
        j += 1
    body, keys, depth = text[i + 1:j], set(), 0
    for m in re.finditer(r"[{}]|(?:[\"']([a-z-]+)[\"']|\b([a-z]+))\s*:", body):
        if m.group(0) in "{}":
            depth += 1 if m.group(0) == "{" else -1
        elif depth == 0:
            keys.add(m.group(1) or m.group(2))
    return keys


class Same(unittest.TestCase):
    def setUp(self):
        self.cats = set(py_value(("app", "identify.py"), "CATEGORIES"))

    def test_the_hik_model_table_files_into_known_categories(self):
        produced = set(re.findall(r'^\s*\(re\.compile\(.*?\), "([a-z-]+)", "', read("app", "identify.py"), re.M))
        self.assertTrue({"access", "intercom", "network", "nvr", "camera"} <= produced)
        self.assertLessEqual(produced, self.cats)

    def test_site_page(self):
        html = read("app", "static", "index.html")
        for name in ("const catStyle = ", "const catIcon = ", "const catLabel = ", "const catAccent = "):
            self.assertEqual(js_keys(html, name), self.cats, name)

    def test_hub_control_center(self):
        self.assertEqual(js_keys(read("hub", "app", "static", "cc", "core.js"), "CC.CATS = "), self.cats)

    def test_hub_classic_site_page(self):
        html = read("hub", "app", "static", "site.html")
        for name in ("const catIcon = ", "const catChip = "):
            self.assertEqual(js_keys(html, name), self.cats, name)
        order = re.search(r"const CAT_ORDER = \[(.*?)\];", html, re.S).group(1)
        self.assertEqual(set(re.findall(r"'([a-z-]+)'", order)), self.cats)

    def test_network_diagram(self):
        self.assertEqual(set(py_value(("app", "topology.py"), "CATEGORY_KIND")), self.cats - {"network", "internet-ap"})


if __name__ == "__main__":
    unittest.main()
