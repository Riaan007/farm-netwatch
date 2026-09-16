"""Network topology: towers and sites (groups), equipment added by hand, the
connections between devices, and where everything sits on the diagram.

ONE store per site Pi (/data/topology.json) and ONE graph builder, so the
site's Network page, its map and the hub's Control Center always draw the same
records. Diagram positions are their own namespace — the GPS position of a
device lives in the device registry (`geo`) and is never touched from here, so
dragging a box on the diagram cannot move a pin on the map.

Records (topology.json)
  groups     {gid: {id, name, kind, description, lat, lon, pos, locked, collapsed}}
  equipment  {vid: {id, name, kind, notes, ports, port_notes, ip, mac, model}}   ids "v-…"
  nodes      {node id: {group, pos, locked, manual, hidden, show, kind, icon}}   device key or "v-…"
  links      {lid: {id, a, a_port, b, b_port, medium, label, notes, source, evidence}}
  dismissed  {suggestion id: ts}
  type_icons {kind: icon id}      icons {icon id: {mime, bytes, name, ts}} (files in topo_icons/)
  view       {lock_all, unassigned_pos}

Positions are RELATIVE to the node's container (its group, or the "Unassigned"
review area) — moving a tower moves its equipment with it in one write.

Connections are only SUGGESTED from evidence that two things are physically
joined: a switch/router port with exactly one device learned on it, or a radio
registered on an access point. Sharing a subnet, a tower or a GPS spot proves
nothing and never creates a link. Hand-added equipment is "not monitored" —
never "offline" because it cannot answer a ping.
"""
import copy
import hashlib
import ipaddress
import json
import os
import re
import secrets
import threading
import time

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
PATH = os.path.join(DATA_DIR, "topology.json")
ICON_DIR = os.path.join(DATA_DIR, "topo_icons")
VERSION = 1
UNASSIGNED = "~"                 # container id of the review area
QUIET_AFTER = 7 * 86400          # same "gone quiet" line as the dashboards

MAX_GROUPS, MAX_EQUIPMENT, MAX_LINKS, MAX_ICONS = 300, 600, 3000, 80
ICON_MAX_BYTES = 300 * 1024

GROUP_KINDS = {
    "tower": "Tower / mast", "site": "Site", "building": "Building",
    "pole": "Pole", "cabinet": "Cabinet / rack", "area": "Area",
}
MEDIA = {"ethernet": "Ethernet", "fibre": "Fibre", "wireless": "Wireless"}

# id, label, layout tier (0 = top of the tower), can hold a wireless link
KINDS = [
    ("internet", "Internet / ISP", 0, False),
    ("ptp", "Point-to-point radio", 0, True),
    ("radio", "Wi-Fi radio / access point", 0, True),
    ("router", "Router / gateway", 1, False),
    ("wifi-router", "Wireless router", 1, True),
    ("switch", "Managed switch", 2, False),
    ("unmanaged-switch", "Unmanaged switch", 2, False),
    ("media-converter", "Fibre media converter", 2, False),
    ("patch-panel", "Patch panel", 2, False),
    ("poe", "PoE injector", 3, False),
    ("nvr", "NVR / recorder", 3, False),
    ("server", "Server / Pi", 3, False),
    ("nas", "NAS / storage", 3, False),
    ("ups", "UPS / battery", 3, False),
    ("camera", "Camera", 4, False),
    ("alarm", "Alarm system", 4, False),
    ("solar", "Solar / inverter", 4, False),
    ("pc", "Computer", 4, False),
    ("printer", "Printer", 4, False),
    ("phone", "Phone / VoIP", 4, False),
    ("media", "TV / media", 4, False),
    ("iot", "Smart device", 4, False),
    ("other", "Other equipment", 4, False),
    ("unknown", "Unidentified device", 4, False),
]
KIND = {k: {"id": k, "label": label, "tier": tier, "wireless": wl} for k, label, tier, wl in KINDS}
WIRELESS_KINDS = {k for k, v in KIND.items() if v["wireless"]}
# What turns up in the review area on its own: the equipment a support contract
# is about. Phones, laptops and TVs only appear once someone adds them.
INFRA_KINDS = {"internet", "ptp", "radio", "router", "wifi-router", "switch", "unmanaged-switch",
               "media-converter", "poe", "nvr", "camera", "alarm", "solar", "ups"}
CATEGORY_KIND = {
    "camera": "camera", "nvr": "nvr", "router": "router", "printer": "printer", "nas": "nas",
    "voip": "phone", "alarm": "alarm", "solar": "solar", "media": "media", "iot": "iot",
    "pc": "pc", "server": "server", "unknown": "unknown",
}

# Diagram grid (px at zoom 1). The browser draws every node inside one cell, so
# the server can lay the diagram out without knowing anything about the DOM.
CELL_W, CELL_H = 150, 122
PAD_X, PAD_TOP, PAD_BOTTOM = 18, 54, 14
ROW_MAX = 6
GAP_X, GAP_Y = 96, 76
COLLAPSED_W, COLLAPSED_H = 200, 124
EMPTY_W, EMPTY_H = 260, 124
UNASSIGNED_ROW = 8
UA_MIN_W = 520                   # the review area is drawn at least this wide (topoview.js too)
UPLINK_MACS = 6                  # switchmon.uplink_ports() uses the same line

_TEXT_RE = re.compile(r"[\x00-\x1f\x7f]")
_RADIO_DISH = re.compile(r"(powerbeam|nanobeam|litebeam|airfiber|gigabeam|isobeam|pbe-|nbe-|lbe-|\baf-?\d|\bwave\b|ptp|prism|rocket)", re.I)
_RADIO_ANY = re.compile(r"(liteap|lap-|nanostation|nsm\d|airmax|bullet|loco|sector|epmp|cambium|mimosa|force ?\d{3})", re.I)
_AP_MODEL = re.compile(r"(\buap\b|\bu6\b|\bu7\b|unifi|ac lite|ac pro|ac lr|nanohd|flexhd|\beap\d|\bcap\b|access point|\bwap\b)", re.I)
_SWITCH_MODEL = re.compile(r"(switch|\bes-\d|\bep-s|uisp-s|\busw|\bcrs\d|\bcss\d|\bgs\d{3}|tl-sg|\bsg\d{3}|rg-es|rg-nbs)", re.I)
_WIRELESS_IFACE = re.compile(r"^(wlan|wifi|cap|wl|ath)", re.I)


# ---- small validators ----------------------------------------------------------
class TopologyError(ValueError):
    """A request the store refuses; the message is shown to the operator."""
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def _s(v):
    """A request field that must be a string; anything else counts as empty."""
    return v if isinstance(v, str) else ""


def _text(v, n):
    return _TEXT_RE.sub(" ", str(v if v is not None else "")).strip()[:n]


def _multiline(v, n):
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(v if v is not None else "")).strip()[:n]


def _float(v, lo, hi):
    try:
        f = float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return round(f, 6) if lo <= f <= hi and f == f else None


def _pos(v):
    if not isinstance(v, dict):
        return None
    x, y = _float(v.get("x"), -200000, 200000), _float(v.get("y"), -200000, 200000)
    return None if x is None or y is None else {"x": round(x, 1), "y": round(y, 1)}


def mac_norm(v):
    h = re.sub(r"[^0-9a-f]", "", str(v or "").lower())
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else ""


def _ip(v):
    s = _text(v, 45)
    if not s:
        return ""
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        raise TopologyError(f"“{s}” is not an IP address — leave it empty if the equipment has none")


def _new_id(prefix, taken):
    while True:
        i = f"{prefix}-{secrets.token_hex(4)}"
        if i not in taken:
            return i


def _latlon(body):
    """(lat, lon) from a body, (None, None) when both are blank; raises on half/invalid."""
    raw_lat, raw_lon = body.get("lat"), body.get("lon")
    if (raw_lat in (None, "")) and (raw_lon in (None, "")):
        return None, None
    lat, lon = _float(raw_lat, -90, 90), _float(raw_lon, -180, 180)
    if lat is None or lon is None or (lat == 0 and lon == 0):
        raise TopologyError("Enter a latitude between -90 and 90 and a longitude between -180 and 180")
    return lat, lon


# ---- the store -----------------------------------------------------------------
def blank():
    return {"version": VERSION, "rev": 0, "updated_ts": 0, "groups": {}, "equipment": {},
            "nodes": {}, "links": {}, "dismissed": {}, "type_icons": {}, "icons": {}, "view": {}}


def normalize(raw):
    """A loaded/imported document with every section present and of the right type."""
    d = blank()
    if isinstance(raw, dict):
        for k, v in d.items():
            got = raw.get(k)
            if isinstance(v, dict) and isinstance(got, dict):
                d[k] = got
            elif isinstance(v, int) and isinstance(got, int):
                d[k] = got
    for sec in ("groups", "equipment", "nodes", "links"):
        d[sec] = {k: v for k, v in d[sec].items() if isinstance(k, str) and isinstance(v, dict)}
    return d


class Store:
    """topology.json, cached in memory. Every change goes through mutate() under
    one lock, so two editors can never overwrite each other's work — each call
    changes only what it names."""

    def __init__(self, path=PATH, icon_dir=ICON_DIR):
        self.path = path
        self.icon_dir = icon_dir
        self.lock = threading.RLock()
        self._doc = None
        self.broken = ""          # why the file could not be read — nothing is saved over it then

    def doc(self):
        with self.lock:
            if self._doc is None:
                self.broken = ""
                try:
                    with open(self.path) as f:
                        self._doc = normalize(json.load(f))
                except FileNotFoundError:
                    self._doc = blank()
                except ValueError:
                    # A damaged file is kept aside, never silently replaced by an empty diagram.
                    bad = f"{self.path}.bad-{int(time.time())}"
                    try:
                        os.replace(self.path, bad)
                        print(f"topology: {self.path} was unreadable — kept as {bad}", flush=True)
                    except OSError as e:
                        self.broken = f"topology.json is damaged and could not be set aside ({e})"
                    self._doc = blank()
                except OSError as e:
                    self.broken = f"topology.json could not be read ({e})"
                    self._doc = blank()
            return self._doc

    def _write(self):
        if self.broken:
            raise TopologyError(f"Not saved: {self.broken}", 503)
        d = self._doc
        d["rev"] = int(d.get("rev") or 0) + 1
        d["updated_ts"] = int(time.time())
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, separators=(",", ":"))
        os.replace(tmp, self.path)

    def mutate(self, fn, *a, **kw):
        """Run fn(doc, …) under the lock on a COPY and keep it only when fn returns
        without raising — an edit that fails halfway leaves nothing behind. A
        function that returns the sentinel NOCHANGE skips the write."""
        with self.lock:
            work = copy.deepcopy(self.doc())
            out = fn(work, *a, **kw)
            if out is NOCHANGE:
                return None
            previous, self._doc = self._doc, work
            try:
                self._write()
            except Exception:
                self._doc = previous
                raise
            return out

    def replace(self, raw):
        with self.lock:
            self._doc = normalize(raw)
            self._write()

    def reload(self):
        with self.lock:
            self._doc = None

    # ---- icon files ----------------------------------------------------------
    def icon_path(self, iid):
        if not re.fullmatch(r"i-[0-9a-f]{8}", iid or ""):
            return None
        return os.path.join(self.icon_dir, iid)


NOCHANGE = object()
store = Store()


# ---- groups -----------------------------------------------------------------------
def create_group(d, body):
    name = _text(body.get("name"), 80)
    if not name:
        raise TopologyError("Give the group a name, e.g. “Tower A” or “Farm office”")
    if len(d["groups"]) >= MAX_GROUPS:
        raise TopologyError(f"This site already has {MAX_GROUPS} groups")
    kind = _s(body.get("kind")) if _s(body.get("kind")) in GROUP_KINDS else "tower"
    lat, lon = _latlon(body)
    gid = _new_id("g", d["groups"])
    g = {"id": gid, "name": name, "kind": kind,
         "description": _multiline(body.get("description"), 600),
         "lat": lat, "lon": lon, "locked": False, "collapsed": False,
         "created_ts": int(time.time())}
    p = _pos(body.get("pos"))
    if p:
        g["pos"] = p              # where the operator was looking — still free to be nudged
        g["manual"] = False
    d["groups"][gid] = g
    return g


def update_group(d, gid, body):
    g = d["groups"].get(gid)
    if not g:
        raise TopologyError("That group no longer exists", 404)
    if "name" in body:
        name = _text(body.get("name"), 80)
        if not name:
            raise TopologyError("A group needs a name")
        g["name"] = name
    if _s(body.get("kind")) in GROUP_KINDS:
        g["kind"] = body["kind"]
    if "description" in body:
        g["description"] = _multiline(body.get("description"), 600)
    if body.get("clear_location"):
        g["lat"] = g["lon"] = None
    elif "lat" in body or "lon" in body:
        g["lat"], g["lon"] = _latlon(body)
    for flag in ("locked", "collapsed"):
        if flag in body:
            g[flag] = bool(body[flag])
    if "pos" in body:
        p = _pos(body.get("pos"))
        if p:
            g["pos"], g["manual"] = p, True
        else:
            g.pop("pos", None)
            g["manual"] = False
    return g


def delete_group(d, gid):
    """Members are NOT deleted — they go back to the review area."""
    if d["groups"].pop(gid, None) is None:
        raise TopologyError("That group no longer exists", 404)
    moved = 0
    for meta in d["nodes"].values():
        if meta.get("group") == gid:
            meta["group"] = None
            meta.pop("pos", None)
            meta["manual"] = False
            moved += 1
    return {"id": gid, "moved": moved}


# ---- hand-added equipment ---------------------------------------------------------
def _equipment_fields(e, body, creating):
    if creating or "name" in body:
        name = _text(body.get("name"), 80)
        if not name:
            raise TopologyError("Give the equipment a name, e.g. “Gate PoE switch”")
        e["name"] = name
    if creating or "kind" in body:
        kind = _s(body.get("kind")) or "unmanaged-switch"
        if kind not in KIND or kind == "unknown":
            raise TopologyError("Pick what kind of equipment this is")
        e["kind"] = kind
    if creating or "notes" in body:
        e["notes"] = _multiline(body.get("notes"), 1000)
    if creating or "ports" in body:
        raw = body.get("ports")
        if raw in (None, ""):
            e["ports"] = None
        else:
            try:
                n = int(str(raw).strip())
            except ValueError:
                raise TopologyError("Ports must be a whole number (or empty)")
            if not 0 <= n <= 512:
                raise TopologyError("Ports must be between 0 and 512")
            e["ports"] = n
    if creating or "port_notes" in body:
        e["port_notes"] = _multiline(body.get("port_notes"), 300)
    if creating or "model" in body:
        e["model"] = _text(body.get("model"), 80)
    if creating or "ip" in body:
        e["ip"] = _ip(body.get("ip"))
    if creating or "mac" in body:
        raw = _text(body.get("mac"), 40)
        mac = mac_norm(raw)
        if raw and not mac:
            raise TopologyError(f"“{raw}” is not a MAC address — leave it empty if unknown")
        e["mac"] = mac


def create_equipment(d, body):
    if len(d["equipment"]) >= MAX_EQUIPMENT:
        raise TopologyError(f"This site already has {MAX_EQUIPMENT} hand-added items")
    vid = _new_id("v", d["equipment"])
    e = {"id": vid, "created_ts": int(time.time())}
    _equipment_fields(e, body, creating=True)
    d["equipment"][vid] = e
    meta = d["nodes"].setdefault(vid, {})
    gid = _s(body.get("group"))
    if gid and gid != UNASSIGNED:
        if gid not in d["groups"]:
            raise TopologyError("That group no longer exists", 404)
        meta["group"] = gid
    p = _pos(body.get("pos"))
    if p:
        meta["pos"], meta["manual"] = p, True
    return e


def update_equipment(d, vid, body):
    e = d["equipment"].get(vid)
    if not e:
        raise TopologyError("That equipment no longer exists", 404)
    _equipment_fields(e, body, creating=False)
    if "group" in body:
        _set_group(d, vid, body.get("group"), _pos(body.get("pos")))
    return e


def delete_equipment(d, vid):
    if d["equipment"].pop(vid, None) is None:
        raise TopologyError("That equipment no longer exists", 404)
    d["nodes"].pop(vid, None)
    gone = [lid for lid, L in d["links"].items() if vid in (L.get("a"), L.get("b"))]
    for lid in gone:
        d["links"].pop(lid, None)
    return {"id": vid, "links_removed": len(gone)}


# ---- per-node layout + overrides -------------------------------------------------
def _set_group(d, nid, gid, pos=None):
    meta = d["nodes"].setdefault(nid, {})
    gid = _s(gid)
    gid = None if gid in ("", UNASSIGNED) else gid
    if gid and gid not in d["groups"]:
        raise TopologyError("That group no longer exists", 404)
    if meta.get("group") != gid:
        meta["group"] = gid
        if pos:
            meta["pos"], meta["manual"] = pos, True
        else:
            meta.pop("pos", None)
            meta["manual"] = False
    elif pos:
        meta["pos"], meta["manual"] = pos, True
    if gid:
        meta.pop("hidden", None)


def set_node(d, nid, body, known):
    """One node's diagram settings. `known` = ids that exist right now (devices + equipment)."""
    if nid not in known:
        raise TopologyError("Unknown device", 404)
    meta = d["nodes"].setdefault(nid, {})
    if "group" in body:
        _set_group(d, nid, body.get("group"), _pos(body.get("pos")))
    elif "pos" in body:
        p = _pos(body.get("pos"))
        if p:
            meta["pos"], meta["manual"] = p, True
    if "locked" in body:
        meta["locked"] = bool(body["locked"])
    if "hidden" in body:
        if body["hidden"]:
            meta["hidden"] = True
            meta.pop("show", None)
            if not nid.startswith("v-"):
                meta["group"] = None
                meta.pop("pos", None)
        else:
            meta.pop("hidden", None)
    if "show" in body:
        if body["show"]:
            meta["show"] = True
            meta.pop("hidden", None)
        else:
            meta.pop("show", None)
    if "kind" in body:
        k = _s(body.get("kind"))
        if k and k not in KIND:
            raise TopologyError("Unknown equipment type")
        if k:
            meta["kind"] = k
        else:
            meta.pop("kind", None)
    if "icon" in body:
        iid = _s(body.get("icon"))
        if iid and iid not in d["icons"]:
            raise TopologyError("That icon no longer exists", 404)
        if iid:
            meta["icon"] = iid
        else:
            meta.pop("icon", None)
    return meta


def set_layout(d, body, known):
    """Batch of drag results / lock toggles: {nodes:{id:{pos,group,locked}},
    groups:{gid:{pos,locked,collapsed}}, unassigned:{pos}, lock_all}."""
    n = 0
    nodes = body.get("nodes") if isinstance(body.get("nodes"), dict) else {}
    groups = body.get("groups") if isinstance(body.get("groups"), dict) else {}
    for nid, v in nodes.items():
        if nid in known and isinstance(v, dict):
            set_node(d, nid, {k: v[k] for k in ("pos", "group", "locked") if k in v}, known)
            n += 1
    for gid, v in groups.items():
        if gid in d["groups"] and isinstance(v, dict):
            update_group(d, gid, {k: v[k] for k in ("pos", "locked", "collapsed") if k in v})
            n += 1
    ua = body.get("unassigned")
    if isinstance(ua, dict) and "pos" in ua:
        p = _pos(ua.get("pos"))
        if p:
            d["view"]["unassigned_pos"] = p
        else:
            d["view"].pop("unassigned_pos", None)
        n += 1
    if "lock_all" in body:
        d["view"]["lock_all"] = bool(body["lock_all"])
        n += 1
    if "collapse_all" in body:
        for g in d["groups"].values():
            g["collapsed"] = bool(body["collapse_all"])
        n += 1
    return {"changed": n}


# ---- links ---------------------------------------------------------------------------
def _pair(a, b):
    return tuple(sorted((a, b)))


def _node_name(nid, d, devices):
    if nid in d["equipment"]:
        return d["equipment"][nid].get("name") or nid
    dev = devices.get(nid) or {}
    return device_title(dev) if dev else nid


def create_link(d, body, devices, radios=()):
    """devices: {key: device record} for every device that exists right now."""
    a, b = _s(body.get("a")), _s(body.get("b"))
    known = set(devices) | set(d["equipment"])
    if not a or not b:
        raise TopologyError("Pick the two ends of the connection")
    if a == b:
        raise TopologyError("A connection needs two different ends")
    for end in (a, b):
        if end not in known:
            raise TopologyError("One end of that connection no longer exists", 404)
    medium = _s(body.get("medium")) or "ethernet"
    if medium not in MEDIA:
        raise TopologyError("Pick Ethernet, fibre or wireless")
    if medium == "wireless":
        for end in (a, b):
            kind = _kind_with_radios(end, d, devices, radios)
            if kind not in WIRELESS_KINDS:
                raise TopologyError(
                    f"A wireless link joins the two radios themselves — {_node_name(end, d, devices)} is "
                    f"“{KIND.get(kind, KIND['unknown'])['label']}”. Pick the radio it is wired to, or set "
                    "its equipment type to a radio first.")
    if len(d["links"]) >= MAX_LINKS:
        raise TopologyError(f"This site already has {MAX_LINKS} connections")
    a_port, b_port = _text(body.get("a_port"), 24), _text(body.get("b_port"), 24)
    for L in d["links"].values():
        if _pair(L.get("a"), L.get("b")) != _pair(a, b):
            continue
        same_ports = ({(L.get("a"), L.get("a_port") or ""), (L.get("b"), L.get("b_port") or "")}
                      == {(a, a_port), (b, b_port)})
        if same_ports and L.get("medium") == medium:
            raise TopologyError("Those two are already connected that way", 409)
    lid = _new_id("l", d["links"])
    L = {"id": lid, "a": a, "a_port": a_port, "b": b, "b_port": b_port, "medium": medium,
         "label": _text(body.get("label"), 60), "notes": _multiline(body.get("notes"), 500),
         "source": "discovered" if _s(body.get("source")) == "discovered" else "manual",
         "evidence": _text(body.get("evidence"), 300), "created_ts": int(time.time())}
    d["links"][lid] = L
    for end in (a, b):          # a device you wire up is on the diagram from now on
        m = d["nodes"].setdefault(end, {})
        m.pop("hidden", None)
    return L


def update_link(d, lid, body, devices, radios=()):
    L = d["links"].get(lid)
    if not L:
        raise TopologyError("That connection no longer exists", 404)
    if "medium" in body:
        medium = _s(body.get("medium"))
        if medium not in MEDIA:
            raise TopologyError("Pick Ethernet, fibre or wireless")
        if medium == "wireless":
            for end in (L["a"], L["b"]):
                kind = _kind_with_radios(end, d, devices, radios)
                if kind not in WIRELESS_KINDS:
                    raise TopologyError(f"A wireless link joins two radios — {_node_name(end, d, devices)} is not a radio")
        L["medium"] = medium
    for k, n in (("a_port", 24), ("b_port", 24), ("label", 60)):
        if k in body:
            L[k] = _text(body.get(k), n)
    if "notes" in body:
        L["notes"] = _multiline(body.get("notes"), 500)
    if body.get("swap"):
        L["a"], L["b"], L["a_port"], L["b_port"] = L["b"], L["a"], L.get("b_port", ""), L.get("a_port", "")
    return L


def delete_link(d, lid):
    if d["links"].pop(lid, None) is None:
        raise TopologyError("That connection no longer exists", 404)
    return {"id": lid}


def _kind_with_radios(nid, d, devices, radios):
    meta = d["nodes"].get(nid) or {}
    if meta.get("kind") in KIND:
        return meta["kind"]
    if nid in d["equipment"]:
        return d["equipment"][nid].get("kind") or "other"
    dev = devices.get(nid)
    return device_kind(dev, radios=radios) if dev else "unknown"


# ---- suggestions ------------------------------------------------------------------------
def dismiss(d, sid, on=True):
    if not re.fullmatch(r"s-[0-9a-f]{12}", sid or ""):
        raise TopologyError("Unknown suggestion", 404)
    if on:
        d["dismissed"][sid] = int(time.time())
    elif d["dismissed"].pop(sid, None) is None:
        return NOCHANGE
    return {"id": sid, "dismissed": bool(on)}


def _sid(*parts):
    return "s-" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _link_adj(d):
    adj = {}
    for L in d["links"].values():
        adj.setdefault(L["a"], set()).add(L["b"])
        adj.setdefault(L["b"], set()).add(L["a"])
    return adj


def _connected_via_passive(d, a, b, depth=4, adj=None):
    """True when a confirmed path joins a and b directly or only through hand-added
    equipment (an unmanaged switch has no MAC, so the switch table sees the
    camera behind it as if it were plugged straight in)."""
    adj = adj if adj is not None else _link_adj(d)
    seen, frontier = {a}, [a]
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for m in adj.get(n, ()):
                if m == b:
                    return True
                if m not in seen and m in d["equipment"]:
                    seen.add(m)
                    nxt.append(m)
        frontier = nxt
    return False


def suggestions(d, devices, eligible, switches=None, routers=None, radios=None, now=None):
    """Connections the site's own readings prove, not yet on the diagram.

    devices   {key: device}; eligible = ids allowed as endpoints
    switches  {switch key: {"ts", "ports": [{"id", "name", "macs": [mac…]}], "uplinks": {port: n}}}
    routers   {router key: {"ts", "ports": {port name: [mac…]}}}
    radios    radiomon snapshot {key: {ok, ts, mode, ip, links: [{peer, ip, signal, …}]}}
    """
    now = int(now or time.time())
    by_mac, dup = {}, set()
    by_ip = {}
    for k, dev in devices.items():
        m = mac_norm(dev.get("mac") or (k if ":" in k else ""))
        if m:
            if m in by_mac and by_mac[m] != k:
                dup.add(m)          # a proxy-ARP bridge answers for several IPs — not an identity
            by_mac[m] = k
        if dev.get("ip"):
            by_ip.setdefault(dev["ip"], []).append(k)
    for m in dup:
        by_mac.pop(m, None)
    out = []

    def add(s):
        if s["id"] in d["dismissed"]:
            return
        out.append(s)

    kind_of = {k: device_kind(dev, d["nodes"].get(k), radios or {}) for k, dev in devices.items()}
    adj = _link_adj(d)
    table = {"switch": "switch address table", "router": "router bridge table"}

    def wired_from(unit_key, unit_kind, ts, port_rows):
        if unit_key not in eligible:
            return
        unit_name = device_title(devices[unit_key])
        for port_id, port_name, macs, uplink in port_rows:
            if uplink:
                continue                       # many MACs behind it: another switch or a radio link
            macs = list(dict.fromkeys(m for m in (mac_norm(x.get("mac") if isinstance(x, dict) else x)
                                                   for x in macs) if m))
            known = list(dict.fromkeys(k for k in (by_mac.get(m) for m in macs) if k and k != unit_key))
            if not macs or not known:
                continue
            label = port_name if port_name and not re.fullmatch(r"(port\s*)?\d+(/\d+)?", str(port_name), re.I) else ""
            port_txt = f"port {str(port_id).split('/')[-1]}" + (f" ({label})" if label else "")
            inside = [k for k in known if k in eligible]
            network = [k for k in inside if kind_of.get(k) in ("radio", "ptp", "router", "wifi-router", "switch")]
            if len(macs) == 1 or (len(network) == 1 and len(macs) < UPLINK_MACS):
                k = known[0] if len(macs) == 1 else network[0]
                if k not in eligible or _connected_via_passive(d, unit_key, k, adj=adj):
                    continue
                why = (f"{device_title(devices[k])} is the only device {unit_name} sees on {port_txt}"
                       if len(macs) == 1 else
                       f"{device_title(devices[k])} is the only network device {unit_name} sees on {port_txt} — "
                       f"the other {len(macs) - 1} address{'es' if len(macs) > 2 else ''} there are most likely behind it")
                add({"id": _sid("eth", unit_key, port_id, k), "type": "link", "medium": "ethernet",
                     "a": unit_key, "a_port": str(port_id), "b": k, "b_port": "",
                     "source": unit_kind, "ts": ts, "evidence": f"{why} ({table[unit_kind]})"})
            elif inside:
                if all(_connected_via_passive(d, unit_key, k, adj=adj) for k in inside):
                    continue
                add({"id": _sid("shared", unit_key, port_id, ",".join(sorted(inside))), "type": "shared_port",
                     "medium": "ethernet", "a": unit_key, "a_port": str(port_id), "b": "", "b_port": "",
                     "members": inside, "unknown": len(macs) - len(known), "source": unit_kind, "ts": ts,
                     "evidence": f"{len(macs)} devices are learned on {port_txt} of {unit_name} — something without "
                                 "its own address (an unmanaged switch, PoE switch or media converter) sits "
                                 f"between them ({table[unit_kind]})"})

    for skey, sw in (switches or {}).items():
        if skey not in devices:
            continue
        ups = sw.get("uplinks") or {}
        rows = []
        for p in sw.get("ports") or []:
            macs = p.get("macs") or []
            rows.append((p.get("id"), p.get("name") or "", macs,
                         p.get("id") in ups or len(macs) >= UPLINK_MACS))
        wired_from(skey, "switch", sw.get("ts"), rows)
    for rkey, rt in (routers or {}).items():
        if rkey not in devices:
            continue
        rows = [(name, name, macs, len(macs) >= UPLINK_MACS)
                for name, macs in (rt.get("ports") or {}).items()
                if not _WIRELESS_IFACE.match(name or "") and not str(name).startswith("bridge")]
        wired_from(rkey, "router", rt.get("ts"), rows)

    # Radio registrations: the access point's own station table names its peers.
    seen_pairs = set()
    for rkey, r in (radios or {}).items():
        if not r.get("ok") or rkey not in devices:
            continue
        mode = str(((r.get("sample") or {}).get("mode")) or r.get("mode") or "").lower()
        is_ap = mode.startswith("ap") or "access point" in mode
        for ln in r.get("links") or []:
            peer = by_mac.get(mac_norm(ln.get("peer")))
            if not peer and ln.get("ip") and len(by_ip.get(ln["ip"], [])) == 1:
                peer = by_ip[ln["ip"]][0]
            if not peer or peer == rkey:
                continue
            pair = _pair(rkey, peer)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            if rkey not in eligible or peer not in eligible:
                continue
            if any(_pair(L["a"], L["b"]) == pair and L.get("medium") == "wireless" for L in d["links"].values()):
                continue
            ap, sta = (rkey, peer) if is_ap else (peer, rkey)
            bits = []
            if ln.get("signal") is not None:
                bits.append(f"signal {ln['signal']:.0f} dBm")
            if ln.get("distance"):
                bits.append(_km(ln["distance"]))
            add({"id": _sid("wl", *pair), "type": "link", "medium": "wireless",
                 "a": ap, "a_port": "", "b": sta, "b_port": "", "source": "radio", "ts": r.get("ts"),
                 "evidence": f"{device_title(devices[sta])} is registered on {device_title(devices[ap])}"
                             + (f" ({', '.join(bits)})" if bits else "") + " — the radio's own station table"})
    return out


def _km(m):
    try:
        m = float(m)
    except (TypeError, ValueError):
        return ""
    return f"{m / 1000:.1f} km" if m >= 1000 else f"{m:.0f} m"


def accept_suggestion(d, sug, devices, radios=()):
    """Turn a suggestion into a confirmed link (type link) — the link remembers why."""
    if sug.get("type") != "link":
        raise TopologyError("That suggestion needs a decision — open it to choose what to do")
    return create_link(d, {"a": sug["a"], "a_port": sug.get("a_port"), "b": sug["b"],
                           "b_port": sug.get("b_port"), "medium": sug["medium"],
                           "source": "discovered", "evidence": sug.get("evidence")}, devices, radios)


def insert_passive(d, sug, body, devices):
    """A shared-port suggestion resolved by adding the unmanaged switch (or other
    passive box) it implies: unit port → new equipment → each chosen device."""
    if sug.get("type") != "shared_port":
        raise TopologyError("Only a shared-port suggestion can be resolved this way")
    asked = body.get("members") if isinstance(body.get("members"), list) else sug.get("members") or []
    members = [k for k in asked if isinstance(k, str) and k in (sug.get("members") or [])]
    if not members:
        raise TopologyError("Tick at least one device that hangs off that port")
    unit = devices.get(sug["a"]) or {}
    kind = _s(body.get("kind")) if _s(body.get("kind")) in KIND and body.get("kind") != "unknown" else "unmanaged-switch"
    if "group" in body:
        group = _s(body.get("group"))
        group = None if group in ("", UNASSIGNED) else group
    else:
        group = (d["nodes"].get(sug["a"]) or {}).get("group")
    e = create_equipment(d, {
        "name": _text(body.get("name"), 80)
        or f"{KIND[kind]['label']} on {device_title(unit)} port {str(sug.get('a_port')).split('/')[-1]}",
        "kind": kind, "notes": _multiline(body.get("notes"), 1000) or sug.get("evidence", ""),
        "ports": body.get("ports"), "group": group if group in d["groups"] else None})
    create_link(d, {"a": sug["a"], "a_port": sug.get("a_port"), "b": e["id"], "medium": "ethernet",
                    "source": "discovered", "evidence": sug.get("evidence")}, devices)
    for k in members:
        create_link(d, {"a": e["id"], "b": k, "medium": "ethernet", "source": "discovered",
                        "evidence": sug.get("evidence")}, devices)
    d["dismissed"][sug["id"]] = int(time.time())
    return e


# ---- identity changes ---------------------------------------------------------------------
def rekey(d, old, new):
    """A device re-identified under a new key (IP key → MAC key): carry its diagram
    settings and connections over, so nothing is orphaned."""
    if not old or not new or old == new:
        return NOCHANGE
    changed = False
    if old in d["nodes"]:
        meta = d["nodes"].pop(old)
        if new not in d["nodes"]:
            d["nodes"][new] = meta
        changed = True
    for L in d["links"].values():
        for end in ("a", "b"):
            if L.get(end) == old:
                L[end] = new
                changed = True
    return {"old": old, "new": new} if changed else NOCHANGE


def forget(d, keys):
    """Devices were forgotten or pruned on the site: drop their diagram entries and links."""
    keys = {keys} if isinstance(keys, str) else set(keys)
    changed = [k for k in keys if d["nodes"].pop(k, None) is not None]
    gone = [lid for lid, L in d["links"].items() if L.get("a") in keys or L.get("b") in keys]
    for lid in gone:
        d["links"].pop(lid, None)
    return {"keys": sorted(keys), "links_removed": len(gone)} if (changed or gone) else NOCHANGE


def clear_orphans(d, known):
    """Remove connections and diagram settings that point at devices which are not
    on the site any more (a bulk prune keeps them, in case the device returns)."""
    gone_links = [lid for lid, L in d["links"].items() if L.get("a") not in known or L.get("b") not in known]
    for lid in gone_links:
        d["links"].pop(lid, None)
    gone_nodes = [k for k in d["nodes"] if k not in known]
    for k in gone_nodes:
        d["nodes"].pop(k, None)
    if not gone_links and not gone_nodes:
        return NOCHANGE
    return {"links_removed": len(gone_links), "nodes_removed": len(gone_nodes)}


# ---- device knowledge ---------------------------------------------------------------------
def device_title(d):
    for k in ("name", "device_name", "model"):
        if d.get(k):
            return str(d[k])
    t = d.get("type") or ""
    if t and not t.lower().startswith("unknown"):
        return t
    return d.get("hostname") or d.get("ip") or d.get("key") or "Unknown device"


def device_kind(dev, meta=None, radios=()):
    """Which equipment icon a discovered device gets. An operator's choice wins."""
    meta = meta or {}
    if meta.get("kind") in KIND:
        return meta["kind"]
    cat = dev.get("category") or "unknown"
    text = " ".join(str(dev.get(k) or "") for k in ("name", "device_name", "model", "type", "hostname"))
    title = str((dev.get("banner") or {}).get("title") or "")
    vendor = str(dev.get("vendor") or "").lower()
    role = str(dev.get("radio_role") or "").lower()
    if dev.get("is_switch") or re.search(r"edgeswitch|uisp switch", title, re.I):
        return "switch"
    # radiomon tries every Ubiquiti device with a login; only a radio that
    # actually answered with a wireless reading is evidence.
    r = radios.get(dev.get("key")) if isinstance(radios, dict) else None
    if (r and (r.get("ok") or r.get("mode"))) or role:
        if "station" in role or "ptp" in role or _RADIO_DISH.search(text):
            return "ptp"
        return "radio"
    mikrotik = re.search(r"mikrotik|routerboard|routeros", " ".join([text, vendor, title]), re.I)
    if mikrotik:
        return "switch" if re.search(r"\b(crs|css)\d", text, re.I) else "router"
    if cat == "router":
        return "router"
    if cat in ("internet-ap", "network"):
        if _SWITCH_MODEL.search(text) or "switch" in title.lower():
            return "switch"
        if _RADIO_DISH.search(text):
            return "ptp"
        if _RADIO_ANY.search(text) or _AP_MODEL.search(text) or _AP_MODEL.search(title):
            return "radio"
        if cat == "internet-ap":
            return "router"
        if any(v in vendor for v in ("ubiquiti", "ubnt", "cambium", "mimosa", "ruckus", "engenius")):
            return "radio"
        return "switch"
    return CATEGORY_KIND.get(cat, "unknown")


def device_state(dev, now=None):
    now = now or time.time()
    if dev.get("online"):
        return "online"
    ls = dev.get("last_seen") or 0
    return "offline" if ls and now - ls < QUIET_AFTER else "quiet"


# ---- the graph -------------------------------------------------------------------------------
_PROBLEM_TYPES = {"ip_conflict", "wifi_degraded", "switch", "same_mac_multi_ip"}


def _radio_info(r):
    if not r:
        return None
    s = r.get("sample") or {}
    info = {"ok": bool(r.get("ok")), "ts": r.get("ts"), "error": r.get("error") or "",
            "mode": r.get("mode") or s.get("mode") or "", "ssid": r.get("ssid") or s.get("ssid") or "",
            "stations": len(r.get("links") or [])}
    for k in ("freq", "chanbw", "signal", "noise", "airtime", "cap_dl", "cap_ul", "tx_rate", "rx_rate"):
        if s.get(k) is not None:
            info[k] = s[k]
    return info


def _link_metrics(a, b, radios, by_mac_key):
    """What the radios report about the link between a and b (either end's view)."""
    for me, other in ((a, b), (b, a)):
        r = (radios or {}).get(me) or {}
        for ln in r.get("links") or []:
            peer = by_mac_key.get(mac_norm(ln.get("peer")))
            if peer != other and not (ln.get("ip") and ln.get("ip") == by_mac_key.get("@" + other)):
                continue
            m = {"seen_by": me, "ts": r.get("ts")}
            for k in ("signal", "remote_signal", "score_dl", "score_ul", "distance", "latency", "tx", "rx"):
                if ln.get(k) is not None:
                    m[k] = ln[k]
            return m
    return None


def build(d, devices, registry=None, radios=None, switches=None, routers=None, problems=None,
          now=None, scope="infra", gateway=None):
    """Everything both views draw, from ONE document.

    devices   scanner records (list); registry {key: registry entry} (asset radio_role);
    radios    radiomon snapshot["radios"]; switches/routers as for suggestions();
    problems  /api/problems entries. scope "all" puts every seen device in the review area.
    """
    now = int(now or time.time())
    registry = registry or {}
    radios = radios or {}
    devs = {}
    for dev in devices:
        k = dev.get("key")
        if not k:
            continue
        dev = dict(dev)
        role = ((registry.get(k) or {}).get("asset") or {}).get("radio_role")
        if role:
            dev["radio_role"] = role
        devs[k] = dev
    nodes_meta = d["nodes"]
    linked = set()
    for L in d["links"].values():
        linked.add(L.get("a"))
        linked.add(L.get("b"))

    probs = {}
    for p in problems or []:
        if p.get("type") not in _PROBLEM_TYPES:
            continue
        for x in p.get("devices") or []:
            if x.get("key"):
                probs.setdefault(x["key"], []).append({
                    "type": p.get("type"), "severity": p.get("severity"),
                    "detail": p.get("detail") or "", "fix": p.get("fix") or ""})

    # ---- which devices belong on the diagram
    eligible = set(d["equipment"])
    others, hidden = [], []
    kinds = {}
    for k, dev in devs.items():
        meta = nodes_meta.get(k) or {}
        kind = device_kind(dev, meta, radios)
        kinds[k] = kind
        state = device_state(dev, now)
        in_group = bool(meta.get("group")) and meta["group"] in d["groups"]
        if in_group or k in linked:
            eligible.add(k)
        elif meta.get("hidden"):
            hidden.append({"id": k, "name": device_title(dev), "ip": dev.get("ip") or "", "kind": kind, "state": state})
        elif meta.get("show") or scope == "all" or (kind in INFRA_KINDS and state != "quiet"):
            eligible.add(k)
        elif state != "quiet" or dev.get("watch"):
            others.append({"id": k, "name": device_title(dev), "ip": dev.get("ip") or "", "kind": kind, "state": state})

    sugg = suggestions(d, devs, eligible, switches, routers, radios, now)

    by_mac_key = {}
    for k, dev in devs.items():
        m = mac_norm(dev.get("mac"))
        if m:
            by_mac_key[m] = k
        by_mac_key["@" + k] = dev.get("ip")

    nodes = []
    for vid, e in d["equipment"].items():
        meta = nodes_meta.get(vid) or {}
        gid = meta.get("group") if meta.get("group") in d["groups"] else None
        nodes.append({
            "id": vid, "virtual": True, "name": e.get("name") or vid,
            "kind": meta.get("kind") if meta.get("kind") in KIND else (e.get("kind") or "other"),
            "notes": e.get("notes") or "", "ports": e.get("ports"), "port_notes": e.get("port_notes") or "",
            "model": e.get("model") or "", "ip": e.get("ip") or "", "mac": e.get("mac") or "",
            "state": "unmonitored", "group": gid or UNASSIGNED,
            "pos": meta.get("pos"), "locked": bool(meta.get("locked")), "manual": bool(meta.get("manual")),
            "icon": meta.get("icon") or "", "created_ts": e.get("created_ts"),
        })
    for k in eligible:
        dev = devs.get(k)
        if not dev:
            continue
        meta = nodes_meta.get(k) or {}
        gid = meta.get("group") if meta.get("group") in d["groups"] else None
        n = {
            "id": k, "virtual": False, "name": device_title(dev), "kind": kinds[k],
            "category": dev.get("category") or "unknown", "ip": dev.get("ip") or "",
            "mac": dev.get("mac") or "", "vendor": dev.get("vendor") or "", "model": dev.get("model") or "",
            "firmware": dev.get("firmware") or "", "hostname": dev.get("hostname") or "",
            "state": device_state(dev, now), "last_seen": dev.get("last_seen"), "rtt": dev.get("rtt"),
            "watch": bool(dev.get("watch")), "web": 80 in (dev.get("ports") or []) or 443 in (dev.get("ports") or []),
            "geo": (registry.get(k) or {}).get("geo") or dev.get("geo"),
            "group": gid or UNASSIGNED, "pos": meta.get("pos"), "locked": bool(meta.get("locked")),
            "manual": bool(meta.get("manual")), "icon": meta.get("icon") or "",
            "kind_set": meta.get("kind") in KIND,
            "problems": probs.get(k, []),
        }
        if dev.get("switch_port"):
            n["switch_port"] = dev["switch_port"]
        ri = _radio_info(radios.get(k))
        if ri and (ri["ok"] or kinds[k] in WIRELESS_KINDS):   # a failed read on a switch is not radio news
            n["radio"] = ri
        nodes.append(n)
    node_ids = {n["id"] for n in nodes}
    by_id = {n["id"]: n for n in nodes}

    # ---- links with live state
    links, orphans = [], 0
    for L in d["links"].values():
        if L.get("a") not in node_ids or L.get("b") not in node_ids:
            orphans += 1
            continue
        a, b = by_id[L["a"]], by_id[L["b"]]
        mon = [x for x in (a, b) if not x["virtual"]]
        if any(x["state"] in ("offline", "quiet") for x in mon):
            status = "down"
        elif mon:
            status = "up"
        else:
            status = "unknown"
        out = {k: L.get(k, "") for k in ("id", "a", "a_port", "b", "b_port", "medium", "label", "notes",
                                             "source", "evidence")}
        out.update(status=status, confirmed=True, created_ts=L.get("created_ts"))
        if L.get("medium") == "wireless":
            m = _link_metrics(L["a"], L["b"], radios, by_mac_key)
            if m:
                out["metrics"] = m
                if status == "up" and (m.get("signal") is not None and m["signal"] <= -80
                                       or min(m.get("score_dl") or 100, m.get("score_ul") or 100) < 40):
                    out["status"] = "degraded"
        links.append(out)
    for s in sugg:
        if s["type"] != "link":
            continue
        a, b = by_id.get(s["a"]), by_id.get(s["b"])
        if not a or not b:
            continue
        down = any(not x["virtual"] and x["state"] != "online" for x in (a, b))
        out = {"id": s["id"], "a": s["a"], "a_port": s.get("a_port", ""), "b": s["b"], "b_port": s.get("b_port", ""),
               "medium": s["medium"], "label": "", "notes": "", "source": s["source"], "evidence": s["evidence"],
               "status": "down" if down else "up", "confirmed": False, "ts": s.get("ts")}
        if s["medium"] == "wireless":
            m = _link_metrics(s["a"], s["b"], radios, by_mac_key)
            if m:
                out["metrics"] = m
        links.append(out)

    # ---- hand-added equipment: what its neighbours say (labelled as inferred)
    adj = {}
    for L in links:
        if L["confirmed"]:
            adj.setdefault(L["a"], []).append(L["b"])
            adj.setdefault(L["b"], []).append(L["a"])
    for n in nodes:
        if not n["virtual"]:
            continue
        mon = [by_id[m] for m in adj.get(n["id"], []) if not by_id[m]["virtual"]]
        up = sum(1 for m in mon if m["state"] == "online")
        if mon and up:
            n["inferred"] = {"state": "passing", "online": up, "total": len(mon)}
        elif len(mon) >= 2:
            n["inferred"] = {"state": "suspect", "online": 0, "total": len(mon)}

    # ---- suggested group for review-area devices (wired to grouped equipment)
    for L in links:
        if L["medium"] != "ethernet":
            continue
        for me, other in ((L["a"], L["b"]), (L["b"], L["a"])):
            n, o = by_id[me], by_id[other]
            if n["group"] == UNASSIGNED and o["group"] != UNASSIGNED and "suggested_group" not in n:
                n["suggested_group"] = {"group": o["group"], "via": o["name"], "confirmed": L["confirmed"]}

    groups = []
    for g in d["groups"].values():
        members = [n for n in nodes if n["group"] == g["id"]]
        mon = [n for n in members if not n["virtual"]]
        cnt = {"total": len(members), "online": sum(1 for n in mon if n["state"] == "online"),
               "offline": sum(1 for n in mon if n["state"] == "offline"),
               "quiet": sum(1 for n in mon if n["state"] == "quiet"),
               "unmonitored": sum(1 for n in members if n["virtual"]),
               "problems": sum(len(n.get("problems") or []) for n in members)}
        down = cnt["offline"] + cnt["quiet"]
        if mon and down == len(mon):
            status = "down"
        elif down or cnt["problems"]:
            status = "warn"
        elif mon:
            status = "ok"
        else:
            status = "unknown"
        geo, approx = None, False
        if g.get("lat") is not None and g.get("lon") is not None:
            geo = {"lat": g["lat"], "lon": g["lon"]}
        else:
            pts = [n["geo"] for n in members if n.get("geo")]
            if pts:
                geo = {"lat": round(sum(p["lat"] for p in pts) / len(pts), 6),
                       "lon": round(sum(p["lon"] for p in pts) / len(pts), 6)}
                approx = True
        groups.append({**{k: g.get(k) for k in ("id", "name", "kind", "description", "lat", "lon", "pos",
                                                  "created_ts")},
                       "locked": bool(g.get("locked")), "collapsed": bool(g.get("collapsed")),
                       "manual": bool(g.get("manual")),
                       "counts": cnt, "status": status, "geo": geo, "geo_approx": approx})

    core = next((n["id"] for n in nodes if n["kind"] == "internet"), None)
    if not core and gateway:
        core = next((n["id"] for n in nodes if not n["virtual"] and n["ip"] == gateway), None)
    if not core:
        deg = {}
        for L in links:
            for end in (L["a"], L["b"]):
                deg[end] = deg.get(end, 0) + 1
        routers_ = sorted((n for n in nodes if n["kind"] in ("router", "wifi-router") and deg.get(n["id"])),
                          key=lambda n: (-deg[n["id"]], n["name"].lower()))
        core = routers_[0]["id"] if routers_ else None

    graph = {
        "core": core,
        "groups": groups, "nodes": nodes, "links": links,
        "suggestions": [s for s in sugg if s["type"] != "link" or (s["a"] in node_ids and s["b"] in node_ids)],
        "others": sorted(others, key=lambda x: (x["name"].lower(), x["ip"])),
        "hidden": sorted(hidden, key=lambda x: (x["name"].lower(), x["ip"])),
        "orphan_links": orphans,
        "dismissed": len(d["dismissed"]),
    }
    return graph


# ---- layout ---------------------------------------------------------------------------------
def _cell(p):
    return (round((p["y"] - PAD_TOP) / CELL_H), round((p["x"] - PAD_X) / CELL_W))


def _cell_pos(r, c):
    return {"x": float(PAD_X + c * CELL_W), "y": float(PAD_TOP + r * CELL_H)}


def _ideal_rows(members, adj, external, gdepth=None):
    """Rows for a from-scratch layout of one container. Rows follow the network
    downstream: distance from the site's core (the internet router) when the
    container is connected to it, else from the equipment that leads out of the
    container (a tower's backhaul radio). What is not connected goes in rows by
    equipment tier."""
    ids = [m["id"] for m in members]
    idset = set(ids)
    tier = {m["id"]: KIND.get(m["kind"], KIND["other"])["tier"] for m in members}
    name = {m["id"]: (m.get("name") or "").lower() for m in members}
    inner = {i: [j for j in adj.get(i, ()) if j in idset] for i in ids}
    depth = {}
    reached = [i for i in ids if gdepth and i in gdepth]
    if reached:
        levels = sorted({gdepth[i] for i in reached})
        for i in reached:
            depth[i] = levels.index(gdepth[i])
    elif any(inner.values()):
        roots = [i for i in ids if i in external and inner[i]]
        if not roots:
            best = min(tier[i] for i in ids if inner[i])
            roots = [i for i in ids if inner[i] and tier[i] == best]
        for r in roots:
            depth[r] = 0
    frontier = sorted(depth, key=lambda i: depth[i])
    while frontier:                      # inside links reach what the core path did not
        nxt = []
        for i in frontier:
            for j in sorted(inner[i], key=lambda j: (tier[j], name[j])):
                if j not in depth:
                    depth[j] = depth[i] + 1
                    nxt.append(j)
        frontier = nxt
    rows = []
    placed_cols = {}
    for dlev in sorted(set(depth.values())):
        level = [i for i in ids if depth.get(i) == dlev]

        def bary(i):
            ps = [placed_cols[j] for j in inner[i] if j in placed_cols and depth.get(j, 99) < dlev]
            return sum(ps) / len(ps) if ps else 0

        level.sort(key=lambda i: (bary(i), tier[i], name[i]))
        for chunk_start in range(0, len(level), ROW_MAX):
            chunk = level[chunk_start:chunk_start + ROW_MAX]
            for c, i in enumerate(chunk):
                placed_cols[i] = c
            rows.append(chunk)
    rest = [i for i in ids if i not in depth]
    for t in sorted({tier[i] for i in rest}):
        level = sorted([i for i in rest if tier[i] == t], key=lambda i: name[i])
        for chunk_start in range(0, len(level), ROW_MAX):
            rows.append(level[chunk_start:chunk_start + ROW_MAX])
    return rows


def core_depths(graph):
    """Hops from the core (graph["core"]) along every drawn link, or {}."""
    core = graph.get("core")
    if not core:
        return {}
    adj = {}
    for L in graph["links"]:
        adj.setdefault(L["a"], []).append(L["b"])
        adj.setdefault(L["b"], []).append(L["a"])
    depth, frontier = {core: 0}, [core]
    while frontier:
        nxt = []
        for i in frontier:
            for j in adj.get(i, ()):
                if j not in depth:
                    depth[j] = depth[i] + 1
                    nxt.append(j)
        frontier = nxt
    return depth


def _free_cell(taken, r, c):
    """Nearest free cell to (r, c), preferring the same row, then rows below."""
    if (r, c) not in taken:
        return r, c
    for dist in range(1, 60):
        for dr in (0, 1, -1, 2):
            rr = r + dr
            if rr < 0:
                continue
            for cc in (c + dist, c - dist):
                if cc >= 0 and (rr, cc) not in taken and abs(dr) <= dist:
                    return rr, cc
    return r + 60, c


def _tier(m):
    return KIND.get(m.get("kind"), KIND["other"])["tier"]


def _place_members(members, adj, external, force, row_max=ROW_MAX, gdepth=None):
    """Positions for one container. Returns {node id: pos} for nodes that move.

    Hand-moved (manual) and locked members are fixed. Auto-placed members are
    "soft": when new equipment joins the container they are laid out again with
    it, so the group stays tidy — until someone arranges it by hand. A container
    arranged entirely by hand only gets its newcomers, next to what they connect to."""
    out = {}
    if force:
        fixed = [m for m in members if m.get("pos") and m.get("locked")]
    else:
        if all(m.get("pos") for m in members):
            return out
        fixed = [m for m in members if m.get("pos") and (m.get("manual") or m.get("locked"))]
    fixed_ids = {m["id"] for m in fixed}
    movable = [m for m in members if m["id"] not in fixed_ids]
    taken = {_cell(m["pos"]) for m in fixed}
    soft = [m for m in movable if m.get("pos")]
    if force or soft or not fixed:
        rows = _ideal_rows(members, adj, external, gdepth) if row_max == ROW_MAX else _grid_rows(members, row_max)
        movable_ids = {m["id"] for m in movable}
        for r, row in enumerate(rows):
            for c, i in enumerate(row):
                if i not in movable_ids:
                    continue
                rr, cc = _free_cell(taken, r, c)
                taken.add((rr, cc))
                p = _cell_pos(rr, cc)
                cur = next(m for m in movable if m["id"] == i).get("pos")
                if cur != p:
                    out[i] = p
        return out
    # Everything already there was arranged by hand: newcomers go next to what they
    # connect to (under what feeds them, above what they feed), the rest fill a new row.
    by_id = {m["id"]: m for m in members}
    bottom = max(r for r, _ in taken) if taken else -1
    spare_row = None
    for m in sorted(movable, key=lambda m: (_tier(m), (m.get("name") or "").lower(), m["id"])):
        near = [by_id[j] for j in adj.get(m["id"], ()) if j in by_id and by_id[j].get("pos")]
        if near:
            def upstream(o):
                if gdepth and o["id"] in gdepth and m["id"] in gdepth:
                    return gdepth[o["id"]] <= gdepth[m["id"]]
                return _tier(o) <= _tier(m)
            near.sort(key=lambda o: (not upstream(o), _tier(o), o["pos"]["y"], o["pos"]["x"]))
            o = near[0]
            r, c = _cell(o["pos"])
            r = r + 1 if upstream(o) else max(0, r - 1)
            rr, cc = _free_cell(taken, r, c)
        else:
            if spare_row is None:
                spare_row = bottom + 1
            cc = next((c for c in range(row_max) if (spare_row, c) not in taken), None)
            if cc is None:
                spare_row += 1
                cc = 0
            rr = spare_row
        taken.add((rr, cc))
        bottom = max(bottom, rr)
        out[m["id"]] = _cell_pos(rr, cc)
        m["pos"] = out[m["id"]]
    return out


def _grid_rows(members, row_max):
    order = sorted(members, key=lambda m: (KIND.get(m["kind"], KIND["other"])["tier"], (m.get("name") or "").lower(), m["id"]))
    return [[m["id"] for m in order[i:i + row_max]] for i in range(0, len(order), row_max)]


def container_size(members, collapsed=False):
    if collapsed:
        return COLLAPSED_W, COLLAPSED_H
    pts = [m["pos"] for m in members if m.get("pos")]
    if not pts:
        return EMPTY_W, EMPTY_H
    w = max(p["x"] for p in pts) + CELL_W + PAD_X
    h = max(p["y"] for p in pts) + CELL_H + PAD_BOTTOM
    return max(EMPTY_W, round(w)), max(EMPTY_H, round(h))


def _overlaps(a, b, gap=24):
    return not (a[0] + a[2] + gap <= b[0] or b[0] + b[2] + gap <= a[0]
                or a[1] + a[3] + gap <= b[1] or b[1] + b[3] + gap <= a[1])


def layout(d, graph, force=False, scope=None):
    """Give every node and group without a position one (and persist it), so a
    new device never reshuffles what an operator arranged. force=True is the
    explicit Auto-arrange: everything that is not locked is placed afresh.
    scope: None (everything) or a group id / UNASSIGNED — then only the inside
    of that container is arranged.

    Returns {"nodes": {id: pos}, "groups": {gid: pos}, "unassigned": pos|None}."""
    changes = {"nodes": {}, "groups": {}, "unassigned": None}
    nodes = graph["nodes"]
    adj = {}
    for L in graph["links"]:
        adj.setdefault(L["a"], []).append(L["b"])
        adj.setdefault(L["b"], []).append(L["a"])
    by_group = {}
    for n in nodes:
        by_group.setdefault(n["group"], []).append(n)
    group_of = {n["id"]: n["group"] for n in nodes}
    locked_groups = {g["id"] for g in graph["groups"] if g.get("locked")}
    gdepth = core_depths(graph)

    # 1. members inside each container
    for cid, members in by_group.items():
        if scope and cid != scope:
            continue
        if force and cid in locked_groups:
            continue                      # a locked tower keeps its whole arrangement
        external = {n["id"] for n in members
                    if any(group_of.get(j) != cid for j in adj.get(n["id"], ()))}
        row_max = UNASSIGNED_ROW if cid == UNASSIGNED else ROW_MAX
        moved = _place_members(members, adj, external, force, row_max, gdepth)
        for n in members:
            if n["id"] in moved:
                n["pos"] = moved[n["id"]]
        changes["nodes"].update(moved)

    # 2. the containers on the canvas (a scoped tidy-up leaves them where they are)
    groups = graph["groups"]
    gby = {g["id"]: g for g in groups}
    size = {g["id"]: container_size(by_group.get(g["id"], []), g.get("collapsed")) for g in groups}
    gadj = {}
    for L in graph["links"]:
        ga, gb = group_of.get(L["a"]), group_of.get(L["b"])
        if ga and gb and ga != gb and UNASSIGNED not in (ga, gb):
            gadj.setdefault(ga, set()).add(gb)
            gadj.setdefault(gb, set()).add(ga)

    core_group = group_of.get(graph.get("core"))

    def rank(gid):
        g = gby[gid]
        members = by_group.get(gid, [])
        has_router = any(m["kind"] in ("router", "internet") for m in members)
        return (gid != core_group, not has_router, -len(gadj.get(gid, ())), g.get("created_ts") or 0,
                (g.get("name") or "").lower(), gid)

    if scope:
        fixed = [g for g in groups if g.get("pos")]
    elif force:
        fixed = [g for g in groups if g.get("pos") and g.get("locked")]
    else:
        fixed = [g for g in groups if g.get("pos")]
    fixed_ids = {g["id"] for g in fixed}
    todo = [g["id"] for g in groups if g["id"] not in fixed_ids]
    rects = [(g["pos"]["x"], g["pos"]["y"], *size[g["id"]]) for g in fixed]
    ua_members = by_group.get(UNASSIGNED, [])
    ua_pos = d["view"].get("unassigned_pos")
    ua_size = container_size(ua_members) if ua_members else (0, 0)
    ua_size = (max(ua_size[0], UA_MIN_W), ua_size[1]) if ua_members else ua_size
    ua_rect = (ua_pos["x"], ua_pos["y"], *ua_size) if (ua_pos and ua_members and not (force and not scope)) else None

    def drop(gid, x, y):
        w, h = size[gid]
        rect = (float(x), float(y), w, h)
        for _ in range(500):
            hit = next((r for r in rects + ([ua_rect] if ua_rect else []) if _overlaps(rect, r)), None)
            if not hit:
                break
            rect = (rect[0], hit[1] + hit[3] + GAP_Y, w, h)
        rects.append(rect)
        gby[gid]["pos"] = {"x": rect[0], "y": rect[1]}
        changes["groups"][gid] = gby[gid]["pos"]

    if force and not scope:
        # Bands: each connected set of towers flows left to right from its core
        # group (the one with the router), unconnected groups share a last band.
        seen, bands, singles = set(), [], []
        for gid in sorted(todo + list(fixed_ids), key=rank):
            if gid in seen:
                continue
            comp, frontier = {gid: 0}, [gid]
            while frontier:
                nxt = []
                for x in frontier:
                    for y in sorted(gadj.get(x, ()), key=rank):
                        if y not in comp:
                            comp[y] = comp[x] + 1
                            nxt.append(y)
                frontier = nxt
            seen.update(comp)
            if len(comp) == 1:
                singles.append(gid)
            else:
                bands.append(comp)
        if singles:
            bands.append({gid: i % 4 for i, gid in enumerate(singles)})
        y0 = 0.0
        for comp in bands:
            cols = sorted(set(comp.values()))
            widths = {c: max(size[g][0] for g, cc in comp.items() if cc == c) for c in cols}
            xoff, x = {}, 0.0
            for c in cols:
                xoff[c] = x
                x += widths[c] + GAP_X
            band_bottom = y0
            for gid in sorted(comp, key=lambda g: (comp[g], rank(g))):
                if gid in fixed_ids:
                    continue
                drop(gid, xoff[comp[gid]], y0)
                r = rects[-1]
                band_bottom = max(band_bottom, r[1] + r[3])
            y0 = band_bottom + GAP_Y
    else:
        for gid in sorted(todo, key=rank):
            linked = [gby[o] for o in gadj.get(gid, ()) if gby[o].get("pos")]
            if linked:
                o = sorted(linked, key=lambda g: rank(g["id"]))[0]
                drop(gid, o["pos"]["x"] + size[o["id"]][0] + GAP_X, o["pos"]["y"])
            else:
                right = max([r[0] + r[2] for r in rects] or [-GAP_X])
                drop(gid, right + GAP_X, 0.0)

    # 3. groups never overlap: when one grows (equipment added, expanded), the
    # group it would cover moves right or down — whichever is the shorter move.
    rect = {g["id"]: [g["pos"]["x"], g["pos"]["y"], *size[g["id"]]] for g in groups if g.get("pos")}
    order = sorted(rect, key=lambda gid: (rect[gid][1], rect[gid][0], gid))
    for _ in range(4):
        changed = False
        for i, gid in enumerate(order):
            for other in order[:i]:
                a, b = rect[gid], rect[other]
                if not _overlaps(a, b, gap=30):
                    continue
                # the one that yields: never a locked group, preferably not one the
                # operator just placed by hand, else the later one in reading order
                cands = sorted((x for x in (gid, other) if not gby[x].get("locked")),
                               key=lambda x: (bool(gby[x].get("manual")), x != gid))
                if not cands:
                    continue                    # two locked groups: the operator's call
                mover = cands[0]
                anchor = other if mover == gid else gid
                m_, n_ = rect[mover], rect[anchor]
                dx = n_[0] + n_[2] + GAP_X - m_[0]
                dy = n_[1] + n_[3] + GAP_Y - m_[1]
                if 0 < dx <= dy or dy <= 0:
                    m_[0] += max(dx, 0)
                else:
                    m_[1] += dy
                changed = True
        if not changed:
            break
    rects = []
    for gid, r in rect.items():
        p = {"x": float(round(r[0])), "y": float(round(r[1]))}
        if p != gby[gid]["pos"]:
            gby[gid]["pos"] = p
            changes["groups"][gid] = p
        rects.append(tuple(r))

    # 4. the review area sits under everything
    if ua_members:
        bottom = max([r[1] + r[3] for r in rects] or [-GAP_Y])
        want = {"x": 0.0, "y": float(bottom + GAP_Y + 20)}
        if (force and not scope) or not ua_pos:
            changes["unassigned"] = want
        else:
            cur = (ua_pos["x"], ua_pos["y"], *ua_size)
            if any(_overlaps(cur, r) for r in rects):
                changes["unassigned"] = want
    return changes


def apply_layout(d, changes, force=False):
    """Persist layout() output. Returns True when anything changed."""
    changed = False
    for nid, p in changes["nodes"].items():
        meta = d["nodes"].setdefault(nid, {})
        if meta.get("pos") != p:
            meta["pos"] = p
            changed = True
        if force and meta.get("manual"):
            meta["manual"] = False
            changed = True
    for gid, p in changes["groups"].items():
        g = d["groups"].get(gid)
        if g is not None and g.get("pos") != p:
            g["pos"] = p
            if force:
                g["manual"] = False
            changed = True
    if changes.get("unassigned"):
        d["view"]["unassigned_pos"] = changes["unassigned"]
        changed = True
    return changed


def boxes(graph, view):
    """Container rectangles (canvas coords) the browser draws around members."""
    by_group = {}
    for n in graph["nodes"]:
        by_group.setdefault(n["group"], []).append(n)
    for g in graph["groups"]:
        w, h = container_size(by_group.get(g["id"], []), False)
        g["size"] = {"w": w, "h": h}
        g["size_collapsed"] = {"w": COLLAPSED_W, "h": COLLAPSED_H}
    ua = by_group.get(UNASSIGNED, [])
    w, h = container_size(ua, False)
    return {"pos": view.get("unassigned_pos") or {"x": 0, "y": 0}, "size": {"w": max(w, UA_MIN_W), "h": h},
            "count": len(ua)}


# ---- icons ---------------------------------------------------------------------------------
_MIME = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg", b"GIF8": "image/gif"}


def sniff_image(data):
    for magic, mime in _MIME.items():
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def add_icon(d, s, data, name="", kind=None, node=None, known=()):
    """Store an uploaded icon (PNG/JPEG/WebP/GIF only — never SVG, which could
    carry script) and optionally assign it to an equipment type or one node."""
    if len(data) > ICON_MAX_BYTES:
        raise TopologyError(f"Icons are limited to {ICON_MAX_BYTES // 1024} kB — use a smaller picture")
    mime = sniff_image(data)
    if not mime:
        raise TopologyError("Use a PNG, JPEG, WebP or GIF picture")
    if len(d["icons"]) >= MAX_ICONS:
        raise TopologyError(f"This site already has {MAX_ICONS} custom icons — delete one first")
    if kind and kind not in KIND:
        raise TopologyError("Unknown equipment type")
    if node and node not in known:
        raise TopologyError("Unknown device", 404)
    iid = _new_id("i", d["icons"])
    os.makedirs(s.icon_dir, exist_ok=True)
    tmp = s.icon_path(iid) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, s.icon_path(iid))
    d["icons"][iid] = {"id": iid, "mime": mime, "bytes": len(data), "name": _text(name, 60),
                       "ts": int(time.time())}
    if kind:
        d["type_icons"][kind] = iid
    if node:
        d["nodes"].setdefault(node, {})["icon"] = iid
    return d["icons"][iid]


def delete_icon(d, s, iid):
    if d["icons"].pop(iid, None) is None:
        raise TopologyError("That icon no longer exists", 404)
    for k in [k for k, v in d["type_icons"].items() if v == iid]:
        d["type_icons"].pop(k, None)
    for meta in d["nodes"].values():
        if meta.get("icon") == iid:
            meta.pop("icon", None)
    p = s.icon_path(iid)
    try:
        os.remove(p)
    except OSError:
        pass
    return {"id": iid}


def set_type_icon(d, kind, iid):
    if kind not in KIND:
        raise TopologyError("Unknown equipment type")
    if iid:
        if iid not in d["icons"]:
            raise TopologyError("That icon no longer exists", 404)
        d["type_icons"][kind] = iid
    else:
        d["type_icons"].pop(kind, None)
    return {"kind": kind, "icon": iid or ""}


# ---- backup bundle --------------------------------------------------------------------------
def export_bundle(s):
    """The document plus its icon files (base64) for the config export."""
    import base64
    with s.lock:
        doc = json.loads(json.dumps(s.doc()))
    files = {}
    for iid in doc.get("icons") or {}:
        p = s.icon_path(iid)
        try:
            with open(p, "rb") as f:
                files[iid] = base64.b64encode(f.read()).decode()
        except (OSError, TypeError):
            pass
    return {"doc": doc, "icons": files}


def import_bundle(s, bundle):
    import base64
    if not isinstance(bundle, dict) or not isinstance(bundle.get("doc"), dict):
        return False
    doc = normalize(bundle["doc"])
    os.makedirs(s.icon_dir, exist_ok=True)
    for iid, b64 in (bundle.get("icons") or {}).items():
        p = s.icon_path(iid)
        if not p or iid not in doc["icons"]:
            continue
        try:
            data = base64.b64decode(b64)
        except (ValueError, TypeError):
            continue
        if sniff_image(data) and len(data) <= ICON_MAX_BYTES:
            with open(p, "wb") as f:
                f.write(data)
    s.replace(doc)
    return True


def kinds_payload():
    return [{"id": k, "label": v["label"], "tier": v["tier"], "wireless": v["wireless"],
             "infra": k in INFRA_KINDS} for k, v in KIND.items()]
