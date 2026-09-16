"""Network diagram + map records (app/topology.py) and their API
(app/topology_routes.py): groups, hand-added equipment, links, evidence-based
suggestions, the auto-layout and the backup bundle.

Run inside the site image with a throwaway data dir:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_topology.py -v
"""
import base64
import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import topology as T      # noqa: E402

NOW = int(time.time())
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def dev(key, ip, cat, name="", online=True, model="", vendor="", **kw):
    d = {"key": key, "ip": ip, "mac": key if ":" in key else "", "category": cat, "name": name,
         "online": online, "last_seen": NOW if online else NOW - 3600, "model": model, "vendor": vendor}
    d.update(kw)
    return d


# The example site: an office with a router, a managed switch and a PtP radio;
# Tower A with three radios (a PtP back to the office and two sector APs), an
# unmanaged switch nobody can see, and cameras behind it; plus a phone.
DEVICES = [
    dev("aa:00:00:00:00:01", "192.168.0.1", "router", "Office router", vendor="MikroTik", model="RB750"),
    dev("aa:00:00:00:00:02", "192.168.0.2", "network", "Office switch", is_switch=True, model="ES-8-150W", vendor="Ubiquiti"),
    dev("aa:00:00:00:00:03", "192.168.0.3", "network", "Office PtP", model="PowerBeam 5AC", vendor="Ubiquiti"),
    dev("aa:00:00:00:00:04", "192.168.0.4", "network", "Tower A PtP", model="PowerBeam 5AC", vendor="Ubiquiti"),
    dev("aa:00:00:00:00:05", "192.168.0.5", "network", "Tower A North", model="LiteAP AC", vendor="Ubiquiti"),
    dev("aa:00:00:00:00:06", "192.168.0.6", "network", "Tower A South", model="LiteAP AC", vendor="Ubiquiti"),
    dev("aa:00:00:00:00:11", "192.168.0.11", "camera", "Gate cam", vendor="Hikvision"),
    dev("aa:00:00:00:00:12", "192.168.0.12", "camera", "Yard cam", vendor="Hikvision", online=False),
    dev("aa:00:00:00:00:13", "192.168.0.13", "camera", "Dam cam", vendor="Hikvision"),
    dev("aa:00:00:00:00:21", "192.168.0.21", "nvr", "Office NVR", vendor="Hikvision"),
    dev("aa:00:00:00:00:31", "192.168.0.31", "pc", "Phone"),
    {**dev("aa:00:00:00:00:41", "192.168.0.41", "camera", "Old cam", online=False), "last_seen": NOW - 30 * 86400},
]
DEV = {d["key"]: d for d in DEVICES}
SWITCHES = {"aa:00:00:00:00:02": {"ts": NOW, "uplinks": {"0/8": 9}, "ports": [
    {"id": "0/1", "name": "Port 1", "macs": [{"mac": "aa:00:00:00:00:01"}, {"mac": "aa:00:00:00:00:01"}]},  # IPv4+IPv6 rows
    {"id": "0/2", "name": "NVR", "macs": [{"mac": "aa:00:00:00:00:21"}]},
    {"id": "0/3", "name": "Port 3", "macs": ["aa:00:00:00:00:03"]},
    {"id": "0/5", "name": "Port 5", "macs": ["aa:00:00:00:00:11", "aa:00:00:00:00:12", "aa:00:00:00:00:13"]},
    {"id": "0/8", "name": "Port 8", "macs": ["aa:00:00:00:00:%02x" % i for i in range(1, 10)]},
]}}
RADIOS = {
    "aa:00:00:00:00:03": {"ok": True, "ts": NOW, "mode": "Access Point PtP", "sample": {"mode": "ap-ptp-ac", "signal": -58},
                          "links": [{"peer": "AA:00:00:00:00:04", "ip": "192.168.0.4", "signal": -58.0, "distance": 2300.0,
                                     "score_dl": 88.0, "score_ul": 81.0}]},
    "aa:00:00:00:00:05": {"ok": True, "ts": NOW, "mode": "Access Point PtMP", "sample": {"mode": "ap-ptmp-ac"},
                          # the station's wireless MAC differs from its LAN MAC: matched by IP
                          "links": [{"peer": "AA:00:00:00:99:99", "ip": "192.168.0.6", "signal": -64.0}]},
    "aa:00:00:00:00:06": {"ok": False, "error": "ssh refused"},
}


def fresh():
    return T.normalize({})


def graph(d, devices=DEVICES, **kw):
    kw.setdefault("radios", RADIOS)
    kw.setdefault("switches", SWITCHES)
    return T.build(d, devices, {}, now=NOW, **kw)


class Records(unittest.TestCase):
    def test_group_needs_a_name_and_valid_location(self):
        d = fresh()
        with self.assertRaises(T.TopologyError):
            T.create_group(d, {"name": "  "})
        with self.assertRaises(T.TopologyError):
            T.create_group(d, {"name": "Tower A", "lat": "-33.9"})          # half a position
        g = T.create_group(d, {"name": "Tower A", "kind": "tower", "lat": "-33,9", "lon": 18.4, "description": "40 m mast"})
        self.assertEqual((g["lat"], g["lon"], g["kind"]), (-33.9, 18.4, "tower"))
        T.update_group(d, g["id"], {"clear_location": True, "collapsed": True})
        self.assertIsNone(d["groups"][g["id"]]["lat"])
        self.assertTrue(d["groups"][g["id"]]["collapsed"])

    def test_equipment_needs_no_ip_and_is_never_offline(self):
        d = fresh()
        with self.assertRaises(T.TopologyError):
            T.create_equipment(d, {"name": "Sw", "ip": "not-an-ip"})
        e = T.create_equipment(d, {"name": "Gate PoE switch", "kind": "unmanaged-switch", "ports": "8",
                                   "port_notes": "1 uplink, 2-4 cameras", "notes": "in the gate box"})
        self.assertEqual((e["ip"], e["ports"], e["id"][:2]), ("", 8, "v-"))
        n = next(x for x in graph(d)["nodes"] if x["id"] == e["id"])
        self.assertEqual(n["state"], "unmonitored")
        self.assertEqual(n["group"], T.UNASSIGNED)

    def test_deleting_a_group_keeps_its_members(self):
        d = fresh()
        g = T.create_group(d, {"name": "Tower A"})
        e = T.create_equipment(d, {"name": "Sw", "group": g["id"]})
        T.set_node(d, "aa:00:00:00:00:11", {"group": g["id"]}, set(DEV) | {e["id"]})
        self.assertEqual(T.delete_group(d, g["id"])["moved"], 2)
        self.assertIn(e["id"], d["equipment"])
        self.assertIsNone(d["nodes"]["aa:00:00:00:00:11"]["group"])

    def test_wireless_links_join_two_radios(self):
        d = fresh()
        with self.assertRaises(T.TopologyError) as cm:
            T.create_link(d, {"a": "aa:00:00:00:00:04", "b": "aa:00:00:00:00:11", "medium": "wireless"}, DEV, RADIOS)
        self.assertIn("Gate cam", str(cm.exception))
        L = T.create_link(d, {"a": "aa:00:00:00:00:03", "b": "aa:00:00:00:00:04", "medium": "wireless",
                              "label": "5 GHz 2.3 km"}, DEV, RADIOS)
        self.assertEqual(L["medium"], "wireless")
        with self.assertRaises(T.TopologyError) as cm:
            T.create_link(d, {"a": "aa:00:00:00:00:04", "b": "aa:00:00:00:00:03", "medium": "wireless"}, DEV, RADIOS)
        self.assertEqual(cm.exception.status, 409)
        # a hand-added dish counts as a radio
        v = T.create_equipment(d, {"name": "Borehole dish", "kind": "ptp"})
        T.create_link(d, {"a": v["id"], "b": "aa:00:00:00:00:05", "medium": "wireless"}, DEV, RADIOS)
        # the asset form's radio role counts (the routes merge it into the device)
        T.create_link(d, {"a": "aa:00:00:00:00:02", "b": "aa:00:00:00:00:04", "medium": "wireless"},
                      {**DEV, "aa:00:00:00:00:02": {**DEV["aa:00:00:00:00:02"], "is_switch": False,
                                                    "radio_role": "PtP master"}}, RADIOS)
        # an operator can say a device IS a radio
        T.set_node(d, "aa:00:00:00:00:21", {"kind": "radio"}, set(DEV))
        T.create_link(d, {"a": "aa:00:00:00:00:21", "b": "aa:00:00:00:00:06", "medium": "wireless"}, DEV, RADIOS)
        with self.assertRaises(T.TopologyError):
            T.create_link(d, {"a": "aa:00:00:00:00:11", "b": "aa:00:00:00:00:11"}, DEV)
        with self.assertRaises(T.TopologyError):
            T.create_link(d, {"a": "aa:00:00:00:00:11", "b": "nope"}, DEV)

    def test_ports_labels_and_fibre(self):
        d = fresh()
        L = T.create_link(d, {"a": "aa:00:00:00:00:02", "a_port": "SFP1", "b": "aa:00:00:00:00:01", "b_port": "ether1",
                              "medium": "fibre", "label": "300 m single-mode"}, DEV)
        T.update_link(d, L["id"], {"swap": True, "label": "OM3"}, DEV)
        self.assertEqual((L["a"], L["a_port"], L["label"]), ("aa:00:00:00:00:01", "ether1", "OM3"))
        with self.assertRaises(T.TopologyError):
            T.update_link(d, L["id"], {"medium": "wireless"}, DEV)


class Discovery(unittest.TestCase):
    def test_only_proven_connections_are_suggested(self):
        d = fresh()
        g = graph(d)
        s = {(x["a"], x.get("b"), x["type"]): x for x in g["suggestions"]}
        # one device alone on a port → suggested, with the port and the reason
        nvr = s[("aa:00:00:00:00:02", "aa:00:00:00:00:21", "link")]
        self.assertEqual(nvr["a_port"], "0/2")
        self.assertIn("only device", nvr["evidence"])
        # the router's MAC is listed twice (IPv4 + IPv6) — still one device
        self.assertIn(("aa:00:00:00:00:02", "aa:00:00:00:00:01", "link"), s)
        # three cameras on one port → nothing direct, one "something sits between" note
        shared = [x for x in g["suggestions"] if x["type"] == "shared_port"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(set(shared[0]["members"]), {"aa:00:00:00:00:11", "aa:00:00:00:00:12", "aa:00:00:00:00:13"})
        self.assertFalse([x for x in g["suggestions"] if x.get("b") == "aa:00:00:00:00:11"])
        # the uplink port (9 MACs) proves nothing
        self.assertFalse([x for x in g["suggestions"] if x.get("a_port") == "0/8"])
        # radios: by MAC, and by the IP the access point reports
        wl = {(x["a"], x["b"]) for x in g["suggestions"] if x["medium"] == "wireless"}
        self.assertIn(("aa:00:00:00:00:03", "aa:00:00:00:00:04"), wl)
        self.assertIn(("aa:00:00:00:00:05", "aa:00:00:00:00:06"), wl)
        # suggested links are drawn, marked unconfirmed
        drawn = [L for L in g["links"] if not L["confirmed"]]
        self.assertTrue(drawn and all(L["evidence"] for L in drawn))
        # nothing from sharing a subnet: the phone and the cameras have no direct links
        self.assertFalse([L for L in g["links"] if "aa:00:00:00:00:31" in (L["a"], L["b"])])

    def test_accept_dismiss_and_the_unmanaged_switch_between(self):
        d = fresh()
        sugs = {x["id"]: x for x in graph(d)["suggestions"]}
        nvr = next(x for x in sugs.values() if x.get("b") == "aa:00:00:00:00:21")
        L = T.accept_suggestion(d, nvr, DEV, RADIOS)
        self.assertEqual((L["source"], L["a_port"]), ("discovered", "0/2"))
        g = graph(d)
        self.assertFalse([x for x in g["suggestions"] if x["id"] == nvr["id"]])
        self.assertTrue([x for x in g["links"] if x["id"] == L["id"] and x["confirmed"]])
        wl = next(x for x in g["suggestions"] if x["medium"] == "wireless")
        T.dismiss(d, wl["id"])
        self.assertFalse([x for x in graph(d)["suggestions"] if x["id"] == wl["id"]])
        T.dismiss(d, wl["id"], on=False)
        self.assertTrue([x for x in graph(d)["suggestions"] if x["id"] == wl["id"]])
        # resolve the shared port by adding the unmanaged switch it implies
        shared = next(x for x in graph(d)["suggestions"] if x["type"] == "shared_port")
        e = T.insert_passive(d, shared, {"name": "Yard PoE switch", "members": shared["members"]}, DEV)
        g = graph(d)
        self.assertEqual(sum(1 for L in g["links"] if e["id"] in (L["a"], L["b"])), 4)
        self.assertFalse([x for x in g["suggestions"] if x["type"] == "shared_port"])
        # a camera alone behind a modelled unmanaged switch is not "directly on the port"
        sw = {k: {**v, "ports": [{"id": "0/5", "name": "", "macs": ["aa:00:00:00:00:11"]}]} for k, v in SWITCHES.items()}
        self.assertFalse([x for x in graph(d, switches=sw)["suggestions"] if x.get("b") == "aa:00:00:00:00:11"])
        vsw = next(n for n in g["nodes"] if n["id"] == e["id"])
        self.assertEqual(vsw["inferred"]["state"], "passing")     # 2 of its 3 cameras answer

    def test_router_bridge_table(self):
        d = fresh()
        routers = {"aa:00:00:00:00:01": {"ts": NOW, "ports": {"ether2": ["aa:00:00:00:00:02"], "wlan1": ["aa:00:00:00:00:31"],
                                                              "bridge": ["aa:00:00:00:00:13"]}}}
        s = graph(d, switches={}, routers=routers)["suggestions"]
        self.assertEqual([(x["a"], x["a_port"], x["b"]) for x in s if x["medium"] == "ethernet"],
                         [("aa:00:00:00:00:01", "ether2", "aa:00:00:00:00:02")])


class Diagram(unittest.TestCase):
    def test_who_is_on_the_diagram(self):
        d = fresh()
        g = graph(d, radios={}, switches={})
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn("aa:00:00:00:00:12", ids)                    # offline camera: still infrastructure
        self.assertNotIn("aa:00:00:00:00:31", ids)                 # the phone waits in "others"
        self.assertIn("aa:00:00:00:00:31", {o["id"] for o in g["others"]})
        self.assertNotIn("aa:00:00:00:00:41", ids)                 # gone quiet for a month
        T.set_node(d, "aa:00:00:00:00:31", {"show": True}, set(DEV))
        T.set_node(d, "aa:00:00:00:00:13", {"hidden": True}, set(DEV))
        g = graph(d, radios={}, switches={})
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn("aa:00:00:00:00:31", ids)
        self.assertNotIn("aa:00:00:00:00:13", ids)
        self.assertIn("aa:00:00:00:00:13", {h["id"] for h in g["hidden"]})
        # a quiet device that is part of a tower stays visible (that is the fault to see)
        grp = T.create_group(d, {"name": "Tower B"})
        T.set_node(d, "aa:00:00:00:00:41", {"group": grp["id"]}, set(DEV))
        n = next(n for n in graph(d)["nodes"] if n["id"] == "aa:00:00:00:00:41")
        self.assertEqual(n["state"], "quiet")

    def test_kinds(self):
        g = {n["id"]: n["kind"] for n in graph(fresh())["nodes"]}
        self.assertEqual(g["aa:00:00:00:00:01"], "router")
        self.assertEqual(g["aa:00:00:00:00:02"], "switch")
        self.assertEqual(g["aa:00:00:00:00:03"], "ptp")
        self.assertEqual(g["aa:00:00:00:00:05"], "radio")
        self.assertEqual(g["aa:00:00:00:00:11"], "camera")
        self.assertEqual(T.device_kind({"category": "network", "vendor": "Ruijie", "model": ""}), "switch")
        self.assertEqual(T.device_kind({"category": "internet-ap", "vendor": "Cudy"}), "router")

    def test_group_summary_and_location(self):
        d = fresh()
        g = T.create_group(d, {"name": "Tower A"})
        for k in ("aa:00:00:00:00:11", "aa:00:00:00:00:12"):
            T.set_node(d, k, {"group": g["id"]}, set(DEV))
        devs = [dict(x) for x in DEVICES]
        devs[6]["geo"] = {"lat": -33.0, "lon": 18.0}
        devs[7]["geo"] = {"lat": -33.2, "lon": 18.2}
        G = next(x for x in graph(d, devices=devs)["groups"] if x["id"] == g["id"])
        self.assertEqual((G["counts"]["online"], G["counts"]["offline"], G["status"]), (1, 1, "warn"))
        self.assertTrue(G["geo_approx"])
        self.assertAlmostEqual(G["geo"]["lat"], -33.1)

    def test_layout_places_new_and_keeps_manual(self):
        d = fresh()
        ga = T.create_group(d, {"name": "Office"})
        gb = T.create_group(d, {"name": "Tower A"})
        known = set(DEV)
        for k in ("aa:00:00:00:00:01", "aa:00:00:00:00:02", "aa:00:00:00:00:03", "aa:00:00:00:00:21"):
            T.set_node(d, k, {"group": ga["id"]}, known)
        for k in ("aa:00:00:00:00:04", "aa:00:00:00:00:05", "aa:00:00:00:00:06"):
            T.set_node(d, k, {"group": gb["id"]}, known)
        T.create_link(d, {"a": "aa:00:00:00:00:03", "b": "aa:00:00:00:00:04", "medium": "wireless"}, DEV, RADIOS)
        g = graph(d)
        ch = T.layout(d, g)
        T.apply_layout(d, ch)
        # every node and group has a place; groups do not overlap; linked tower sits to the right
        self.assertTrue(all(d["nodes"][n["id"]].get("pos") for n in g["nodes"] if n["id"] in d["nodes"]))
        pa, pb = d["groups"][ga["id"]]["pos"], d["groups"][gb["id"]]["pos"]
        self.assertGreater(pb["x"], pa["x"])
        self.assertIsNotNone(d["view"].get("unassigned_pos"))
        # rows run downstream from the core (the router the switch table puts on port 1):
        # router, then the switch, then what hangs off the switch (the PtP radio and the NVR)
        self.assertEqual(g["core"], "aa:00:00:00:00:01")
        ys = {k: d["nodes"][k]["pos"]["y"] for k in ("aa:00:00:00:00:03", "aa:00:00:00:00:01", "aa:00:00:00:00:02", "aa:00:00:00:00:21")}
        self.assertLess(ys["aa:00:00:00:00:01"], ys["aa:00:00:00:00:02"])
        self.assertLess(ys["aa:00:00:00:00:02"], ys["aa:00:00:00:00:03"])
        self.assertEqual(ys["aa:00:00:00:00:03"], ys["aa:00:00:00:00:21"])
        # the tower's backhaul radio heads its own group
        self.assertLess(d["nodes"]["aa:00:00:00:00:04"]["pos"]["y"], d["nodes"]["aa:00:00:00:00:05"]["pos"]["y"])
        # an operator drags the NVR; a new device must not move it
        T.set_layout(d, {"nodes": {"aa:00:00:00:00:21": {"pos": {"x": 470, "y": 54}}}}, known)
        T.set_node(d, "aa:00:00:00:00:11", {"group": ga["id"]}, known)
        g = graph(d)
        T.apply_layout(d, T.layout(d, g))
        self.assertEqual(d["nodes"]["aa:00:00:00:00:21"]["pos"], {"x": 470.0, "y": 54.0})
        self.assertTrue(d["nodes"]["aa:00:00:00:00:21"]["manual"])
        cam = d["nodes"]["aa:00:00:00:00:11"]["pos"]
        taken = [d["nodes"][k]["pos"] for k in ("aa:00:00:00:00:01", "aa:00:00:00:00:02", "aa:00:00:00:00:03", "aa:00:00:00:00:21")]
        self.assertNotIn(cam, taken)
        # Auto-arrange moves the manual NVR back into the grid, but not a locked node
        T.set_node(d, "aa:00:00:00:00:02", {"pos": {"x": 900, "y": 900}, "locked": True}, known)
        g = graph(d)
        T.apply_layout(d, T.layout(d, g, force=True), force=True)
        self.assertEqual(d["nodes"]["aa:00:00:00:00:02"]["pos"], {"x": 900.0, "y": 900.0})
        self.assertNotEqual(d["nodes"]["aa:00:00:00:00:21"]["pos"], {"x": 470.0, "y": 54.0})
        self.assertFalse(d["nodes"]["aa:00:00:00:00:21"].get("manual"))
        # a locked group keeps its place and its inside
        T.update_group(d, gb["id"], {"locked": True, "pos": {"x": 5000, "y": 10}})
        inside = {k: dict(d["nodes"][k]["pos"]) for k in ("aa:00:00:00:00:04", "aa:00:00:00:00:05")}
        g = graph(d)
        T.apply_layout(d, T.layout(d, g, force=True), force=True)
        self.assertEqual(d["groups"][gb["id"]]["pos"], {"x": 5000.0, "y": 10.0})
        self.assertEqual({k: d["nodes"][k]["pos"] for k in inside}, inside)
        # moving on the diagram never touches GPS
        self.assertNotIn("geo", d["nodes"]["aa:00:00:00:00:21"])

    def test_a_growing_group_pushes_its_neighbour_aside(self):
        d = fresh()
        a = T.create_group(d, {"name": "Office", "pos": {"x": 0, "y": 0}})
        b = T.create_group(d, {"name": "Tower", "pos": {"x": 300, "y": 0}})
        T.apply_layout(d, T.layout(d, graph(d, radios={}, switches={})))
        self.assertEqual(d["groups"][b["id"]]["pos"], {"x": 300, "y": 0})       # nothing to move yet
        known = set(DEV)
        for k in ("aa:00:00:00:00:11", "aa:00:00:00:00:12", "aa:00:00:00:00:13", "aa:00:00:00:00:21"):
            T.set_node(d, k, {"group": a["id"]}, known)
        g = graph(d, radios={}, switches={})
        T.apply_layout(d, T.layout(d, g))
        g = graph(d, radios={}, switches={})
        T.boxes(g, d["view"])
        ra, rb = [(x["pos"]["x"], x["pos"]["y"], x["size"]["w"], x["size"]["h"]) for x in g["groups"]]
        self.assertFalse(T._overlaps(ra, rb, gap=0), (ra, rb))
        self.assertGreater(d["groups"][b["id"]]["pos"]["x"], 300)
        # a locked neighbour stays put; the grown group is the one that yields
        T.update_group(d, b["id"], {"locked": True, "pos": {"x": 200, "y": 0}})
        g = graph(d, radios={}, switches={})
        T.apply_layout(d, T.layout(d, g))
        self.assertEqual(d["groups"][b["id"]]["pos"], {"x": 200.0, "y": 0.0})
        g = graph(d, radios={}, switches={})
        T.boxes(g, d["view"])
        ra, rb = [(x["pos"]["x"], x["pos"]["y"], x["size"]["w"], x["size"]["h"]) for x in g["groups"]]
        self.assertFalse(T._overlaps(ra, rb, gap=0), (ra, rb))

    def test_many_newcomers_fill_rows(self):
        d = fresh()
        grp = T.create_group(d, {"name": "Tower"})
        known = set(DEV)
        T.set_node(d, "aa:00:00:00:00:02", {"group": grp["id"], "pos": {"x": 18, "y": 54}}, known)   # hand-placed
        cams = [k for k, v in DEV.items() if v["category"] == "camera" and k != "aa:00:00:00:00:41"]
        for k in cams:
            T.set_node(d, k, {"group": grp["id"]}, known)
        g = graph(d, radios={}, switches={})
        T.apply_layout(d, T.layout(d, g))
        rows = {d["nodes"][k]["pos"]["y"] for k in cams}
        self.assertEqual(len(rows), 1, rows)                   # three cameras share one row
        self.assertEqual(d["nodes"]["aa:00:00:00:00:02"]["pos"], {"x": 18, "y": 54})
        # auto-placed members make room when more arrive; the hand-placed switch never moves
        T.set_node(d, "aa:00:00:00:00:21", {"group": grp["id"]}, known)
        g = graph(d, radios={}, switches={})
        T.apply_layout(d, T.layout(d, g))
        pos = [tuple(d["nodes"][k]["pos"].values()) for k in cams + ["aa:00:00:00:00:21", "aa:00:00:00:00:02"]]
        self.assertEqual(len(pos), len(set(pos)))
        self.assertEqual(d["nodes"]["aa:00:00:00:00:02"]["pos"], {"x": 18, "y": 54})

    def test_core_is_the_gateway_or_an_isp_box(self):
        d = fresh()
        self.assertEqual(T.build(d, DEVICES, {}, now=NOW, gateway="192.168.0.21")["core"], "aa:00:00:00:00:21")
        isp = T.create_equipment(d, {"name": "Fibre ONT", "kind": "internet"})
        self.assertEqual(T.build(d, DEVICES, {}, now=NOW, gateway="192.168.0.21")["core"], isp["id"])

    def test_groups_never_overlap_when_many_are_added(self):
        d = fresh()
        known = set(DEV)
        keys = [k for k in DEV if k not in ("aa:00:00:00:00:31", "aa:00:00:00:00:41")]
        for i, k in enumerate(keys):
            g = T.create_group(d, {"name": f"G{i}"})
            T.set_node(d, k, {"group": g["id"]}, known)
        g = graph(d)
        T.apply_layout(d, T.layout(d, g, force=True), force=True)
        g = graph(d)
        T.boxes(g, d["view"])
        rects = [(x["pos"]["x"], x["pos"]["y"], x["size"]["w"], x["size"]["h"]) for x in g["groups"]]
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                self.assertFalse(T._overlaps(a, b, gap=0), (a, b))

    def test_rekey_and_forget(self):
        d = fresh()
        T.set_node(d, "192.168.0.50", {"group": None, "locked": True}, {"192.168.0.50"})
        v = T.create_equipment(d, {"name": "Sw"})
        T.create_link(d, {"a": v["id"], "b": "192.168.0.50"}, {"192.168.0.50": {"key": "192.168.0.50"}})
        T.rekey(d, "192.168.0.50", "bb:00:00:00:00:50")
        self.assertIn("bb:00:00:00:00:50", d["nodes"])
        self.assertEqual(next(iter(d["links"].values()))["b"], "bb:00:00:00:00:50")
        self.assertIs(T.rekey(d, "x", "y"), T.NOCHANGE)
        T.forget(d, "bb:00:00:00:00:50")
        self.assertFalse(d["links"])
        self.assertNotIn("bb:00:00:00:00:50", d["nodes"])


class Api(unittest.TestCase):
    """Through the real Flask app: open reads, locked writes, graph replies, backup."""

    @classmethod
    def setUpClass(cls):
        import config
        import radiomon
        import server
        import siteauth
        import switchmon
        cls.server, cls.siteauth = server, siteauth
        cfg = config.load()
        cfg["configured"] = True
        cfg["site"]["name"] = "Test farm"
        config.save(cfg)
        siteauth.set_hub_key("k" * 43)
        server.scanner.devices = {d["key"]: dict(d) for d in DEVICES}
        radiomon.monitor._warmed = True
        radiomon.monitor._last = dict(RADIOS)
        switchmon.monitor._last = {k: {"ok": True, "ts": NOW, "snap": v} for k, v in SWITCHES.items()}
        T.store.replace({})

    def setUp(self):
        self.c = self.server.app.test_client()
        self.h = {self.siteauth.HEADER: "k" * 43}

    def test_read_open_write_locked(self):
        r = self.c.get("/api/topology")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["can_edit"])
        self.assertEqual(j["site"]["name"], "Test farm")
        self.assertTrue(j["nodes"] and j["suggestions"])
        self.assertEqual(self.c.post("/api/topology/groups", json={"name": "X"}).status_code, 401)
        self.assertEqual(self.c.post("/api/topology/links", json={}).status_code, 401)
        self.assertEqual(self.c.post("/api/topology/arrange", json={}).status_code, 401)
        self.assertTrue(self.c.get("/api/topology", headers=self.h).get_json()["can_edit"])
        rev = j["rev"]
        same = self.c.get(f"/api/topology?since={rev}").get_json()
        self.assertFalse(same["changed"])

    def test_workflow_through_the_api(self):
        h = self.h
        g = self.c.post("/api/topology/groups?graph=1", json={"name": "Tower A", "kind": "tower"}, headers=h).get_json()
        gid = g["result"]["id"]
        self.assertTrue(any(x["id"] == gid for x in g["graph"]["groups"]))
        bad = self.c.post("/api/topology/equipment", json={"name": ""}, headers=h)
        self.assertEqual(bad.status_code, 400)
        self.assertIn("name", bad.get_json()["error"])
        e = self.c.post("/api/topology/equipment", json={"name": "Mast switch", "group": gid}, headers=h).get_json()["result"]
        r = self.c.post(f"/api/topology/nodes/{'aa:00:00:00:00:05'}", json={"group": gid}, headers=h)
        self.assertEqual(r.status_code, 200)
        L = self.c.post("/api/topology/links?graph=1", json={"a": e["id"], "b": "aa:00:00:00:00:05", "b_port": "eth0"},
                        headers=h).get_json()
        self.assertTrue(any(x["id"] == L["result"]["id"] and x["confirmed"] for x in L["graph"]["links"]))
        wl = self.c.post("/api/topology/links", json={"a": e["id"], "b": "aa:00:00:00:00:04", "medium": "wireless"}, headers=h)
        self.assertEqual(wl.status_code, 400)
        sug = next(s for s in L["graph"]["suggestions"] if s.get("medium") == "wireless")
        acc = self.c.post(f"/api/topology/suggestions/{sug['id']}?graph=1", json={"action": "accept"}, headers=h).get_json()
        self.assertTrue(acc["ok"])
        self.assertEqual(acc["result"]["source"], "discovered")
        lay = self.c.post("/api/topology/layout", headers=h, json={
            "nodes": {e["id"]: {"pos": {"x": 18, "y": 300}, "locked": True}},
            "groups": {gid: {"pos": {"x": 1200, "y": 0}, "collapsed": True}}}).get_json()
        self.assertEqual(lay["result"]["changed"], 2)
        arr = self.c.post("/api/topology/arrange", json={}, headers=h).get_json()
        node = next(n for n in arr["graph"]["nodes"] if n["id"] == e["id"])
        self.assertEqual(node["pos"], {"x": 18.0, "y": 300.0})
        # the backup carries the diagram and restores it
        exp = self.c.get("/api/config/export", headers=h).get_json()
        self.assertIn(gid, exp["topology"]["doc"]["groups"])
        T.store.replace({})
        imp = self.c.post("/api/config/import", json=exp, headers=h).get_json()
        self.assertIn("network diagram", imp["applied"])
        self.assertIn(gid, T.store.doc()["groups"])

    def test_icons(self):
        h = self.h
        svg = base64.b64encode(b"<svg onload='alert(1)'></svg>").decode()
        self.assertEqual(self.c.post("/api/topology/icons", json={"data": svg}, headers=h).status_code, 400)
        big = base64.b64encode(PNG + b"0" * (T.ICON_MAX_BYTES + 10)).decode()
        self.assertEqual(self.c.post("/api/topology/icons", json={"data": big}, headers=h).status_code, 400)
        r = self.c.post("/api/topology/icons", json={"data": base64.b64encode(PNG).decode(), "kind": "camera",
                                                      "name": "cam.png"}, headers=h).get_json()
        iid = r["result"]["id"]
        self.assertEqual(T.store.doc()["type_icons"]["camera"], iid)
        got = self.c.get(f"/api/topology/icons/{iid}")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.mimetype, "image/png")
        self.assertIn("sandbox", got.headers["Content-Security-Policy"])
        got.close()
        self.assertEqual(self.c.get("/api/topology/icons/../../etc").status_code, 404)
        n = self.c.post("/api/topology/nodes/aa:00:00:00:00:11", json={"icon": iid}, headers=h)
        self.assertEqual(n.status_code, 200)
        self.assertEqual(self.c.delete(f"/api/topology/icons/{iid}", headers=h).status_code, 200)
        self.assertNotIn("icon", T.store.doc()["nodes"]["aa:00:00:00:00:11"])
        self.assertNotIn("camera", T.store.doc()["type_icons"])

    def test_pruning_drops_links_of_pruned_devices(self):
        h = self.h
        self.c.post("/api/topology/links", json={"a": "aa:00:00:00:00:02", "b": "aa:00:00:00:00:12"}, headers=h)
        removed = self.server.scanner.prune_devices(days=None, only_offline=True)
        self.assertIn("aa:00:00:00:00:12", removed)
        self.assertFalse([L for L in T.store.doc()["links"].values() if "aa:00:00:00:00:12" in (L["a"], L["b"])])
        for k in removed:
            self.server.scanner.devices[k] = dict(DEV[k])

    def test_forgetting_a_device_drops_its_links(self):
        h = self.h
        self.c.post("/api/topology/links", json={"a": "aa:00:00:00:00:02", "b": "aa:00:00:00:00:13"}, headers=h)
        self.server.scanner.delete_device("aa:00:00:00:00:13")
        self.assertFalse([L for L in T.store.doc()["links"].values() if "aa:00:00:00:00:13" in (L["a"], L["b"])])
        self.server.scanner.devices["aa:00:00:00:00:13"] = dict(DEV["aa:00:00:00:00:13"])


if __name__ == "__main__":
    unittest.main()
