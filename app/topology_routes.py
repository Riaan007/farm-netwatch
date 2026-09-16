"""HTTP API behind the Network page (diagram + map) — see app/topology.py.

Reads are open like /api/devices. Every change needs the site login or the hub
key, and every change is one small named operation (move this node, add that
link), so two people editing at once never overwrite each other's work.
Writes answer with the fresh graph when asked (?graph=1), so the browser draws
the result of its own change without a second round trip over a farm link.
"""
import base64
import time

from flask import Blueprint, jsonify, request, send_file, session

import config
import edgeswitch
import radiomon
import siteauth
import switchmon
import topology
from scanner import default_gateway, scanner

bp = Blueprint("topology", __name__)
guard = siteauth.required
store = topology.store

# Filled in by server.py (it owns the router cache and the problem feed).
providers = {
    "routers": lambda: {},
    "problems": lambda: [],
}


def _hooks():
    """The scanner re-keys (IP → MAC) and forgets devices; the diagram follows."""
    def on_rekey(old, new):
        try:
            store.mutate(topology.rekey, old, new)
        except Exception as e:  # noqa: BLE001 - a scan must never fail on the diagram
            print("topology rekey:", e, flush=True)

    def on_forget(keys):
        try:
            store.mutate(topology.forget, keys)
        except Exception as e:  # noqa: BLE001
            print("topology forget:", e, flush=True)
    scanner.on_rekey = on_rekey
    scanner.on_forget = on_forget


_hooks()


# ---- inputs ------------------------------------------------------------------------
def _devices():
    reg = scanner.registry
    out = []
    for d in scanner.get_devices():
        d = dict(d)
        d["geo"] = (reg.get(d.get("key"), {}) or {}).get("geo") or None
        if edgeswitch.is_edgeswitch(d):
            d["is_switch"] = True
        out.append(d)
    return out


def _switches():
    out = {}
    for key, row in (switchmon.monitor.snapshot().get("switches") or {}).items():
        snap = (row or {}).get("snap") or {}
        if snap.get("ports"):
            out[key] = {"ts": snap.get("ts"), "ports": snap.get("ports"), "uplinks": snap.get("uplinks") or {}}
    return out


def _safe(fn, default):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - one broken feed must not blank the diagram
        print("topology feed:", e, flush=True)
        return default


_GW = {"ts": 0.0, "ip": None}


def _gateway():
    """The site's default gateway — the core of the diagram (cached; it is a subprocess)."""
    if time.time() - _GW["ts"] > 120:
        _GW["ip"] = _safe(default_gateway, None)
        _GW["ts"] = time.time()
    return _GW["ip"]


def _inputs():
    return {
        "gateway": _gateway(),
        "devices": _devices(),
        "registry": scanner.registry,
        "radios": _safe(lambda: radiomon.monitor.snapshot()["radios"], {}),
        "switches": _safe(_switches, {}),
        "routers": _safe(providers["routers"], {}),
        "problems": _safe(providers["problems"], []),
    }


def _known(inp, d):
    return {x["key"] for x in inp["devices"] if x.get("key")} | set(d["equipment"])


def _dev_map(inp):
    return {x["key"]: x for x in inp["devices"] if x.get("key")}


def _graph_payload(force=False, scope=None, inp=None):
    inp = inp or _inputs()
    now = int(time.time())
    view_scope = "all" if request.args.get("scope") == "all" else "infra"
    with store.lock:
        d = store.doc()
        g = topology.build(d, inp["devices"], inp["registry"], inp["radios"], inp["switches"],
                           inp["routers"], inp["problems"], now=now, scope=view_scope,
                           gateway=inp.get("gateway"))
        ch = topology.layout(d, g, force=force, scope=scope)
        if topology.apply_layout(d, ch, force=force):
            store._write()
        ua = topology.boxes(g, d["view"])
        cfg_site = (config.load().get("site") or {})
        payload = {
            "ok": True, "rev": d["rev"], "updated_ts": d["updated_ts"], "ts": now,
            "site": {k: cfg_site.get(k) for k in ("name", "location", "lat", "lon")},
            "kinds": topology.kinds_payload(),
            "group_kinds": topology.GROUP_KINDS, "media": topology.MEDIA,
            "grid": {"cell_w": topology.CELL_W, "cell_h": topology.CELL_H,
                     "pad_x": topology.PAD_X, "pad_top": topology.PAD_TOP,
                     "pad_bottom": topology.PAD_BOTTOM, "row_max": topology.ROW_MAX,
                     "empty_w": topology.EMPTY_W, "empty_h": topology.EMPTY_H,
                     "collapsed_w": topology.COLLAPSED_W, "collapsed_h": topology.COLLAPSED_H},
            "core": g.get("core"),
            "groups": g["groups"], "nodes": g["nodes"], "links": g["links"],
            "suggestions": g["suggestions"], "others": g["others"], "hidden": g["hidden"],
            "orphan_links": g["orphan_links"], "dismissed": g["dismissed"],
            "unassigned": ua, "view": {"lock_all": bool(d["view"].get("lock_all"))},
            "icons": dict(d["icons"]), "type_icons": dict(d["type_icons"]),
            "can_edit": siteauth.allowed(request, session),
            "scope": view_scope,
        }
    return payload


def _fail(e):
    return jsonify({"ok": False, "error": str(e)}), getattr(e, "status", 400)


def _reply(result, **extra):
    out = {"ok": True, "result": result, **extra}
    if request.args.get("graph") == "1":
        out["graph"] = _graph_payload()
    else:
        out["rev"] = store.doc()["rev"]
    return jsonify(out)


def _body():
    b = request.get_json(force=True, silent=True)
    return b if isinstance(b, dict) else {}


# ---- read --------------------------------------------------------------------------
@bp.route("/api/topology")
def api_topology():
    """The whole diagram + map: groups, nodes (devices + hand-added equipment) with
    live state, links (confirmed + suggested), the review area and icons.
    ?since=<rev> answers {changed:false} cheaply when nothing moved."""
    since = request.args.get("since")
    if since and since.isdigit() and request.args.get("live") != "1":
        if int(since) == store.doc()["rev"]:
            return jsonify({"ok": True, "changed": False, "rev": int(since)})
    return jsonify(_graph_payload())


# ---- groups ------------------------------------------------------------------------
@bp.route("/api/topology/groups", methods=["POST"])
@guard
def api_group_create():
    try:
        return _reply(store.mutate(topology.create_group, _body()))
    except topology.TopologyError as e:
        return _fail(e)


@bp.route("/api/topology/groups/<gid>", methods=["POST", "DELETE"])
@guard
def api_group(gid):
    try:
        if request.method == "DELETE":
            return _reply(store.mutate(topology.delete_group, gid))
        return _reply(store.mutate(topology.update_group, gid, _body()))
    except topology.TopologyError as e:
        return _fail(e)


# ---- hand-added equipment ----------------------------------------------------------
@bp.route("/api/topology/equipment", methods=["POST"])
@guard
def api_equipment_create():
    try:
        return _reply(store.mutate(topology.create_equipment, _body()))
    except topology.TopologyError as e:
        return _fail(e)


@bp.route("/api/topology/equipment/<vid>", methods=["POST", "DELETE"])
@guard
def api_equipment(vid):
    try:
        if request.method == "DELETE":
            return _reply(store.mutate(topology.delete_equipment, vid))
        return _reply(store.mutate(topology.update_equipment, vid, _body()))
    except topology.TopologyError as e:
        return _fail(e)


def _replace(d, vid, key, devices):
    """Hand-added equipment turned out to be a discovered device (it got an IP):
    the device takes over its place, group, lock, icon and connections."""
    e = d["equipment"].get(vid)
    if not e:
        raise topology.TopologyError("That equipment no longer exists", 404)
    if key not in devices:
        raise topology.TopologyError("Unknown device", 404)
    vmeta = d["nodes"].pop(vid, {}) or {}
    dmeta = d["nodes"].setdefault(key, {})
    for k in ("group", "pos", "locked", "manual", "icon"):
        if k in vmeta and (k not in dmeta or not dmeta.get("group")):
            dmeta[k] = vmeta[k]
    dmeta.pop("hidden", None)
    moved = 0
    for L in list(d["links"].values()):
        for end in ("a", "b"):
            if L.get(end) == vid:
                L[end] = key
                moved += 1
        if L.get("a") == L.get("b"):
            d["links"].pop(L["id"], None)
    d["equipment"].pop(vid, None)
    return {"id": vid, "device": key, "links": moved}


@bp.route("/api/topology/equipment/<vid>/replace", methods=["POST"])
@guard
def api_equipment_replace(vid):
    body = _body()
    try:
        devs = _dev_map({"devices": _devices()})
        return _reply(store.mutate(_replace, vid, str(body.get("device") or ""), devs))
    except topology.TopologyError as e:
        return _fail(e)


# ---- nodes + layout ---------------------------------------------------------------
@bp.route("/api/topology/nodes/<path:nid>", methods=["POST"])
@guard
def api_node(nid):
    inp = {"devices": _devices()}
    try:
        return _reply(store.mutate(lambda d: topology.set_node(d, nid, _body(), _known(inp, d))))
    except topology.TopologyError as e:
        return _fail(e)


@bp.route("/api/topology/layout", methods=["POST"])
@guard
def api_layout():
    """Drag results and lock toggles, batched: {nodes, groups, unassigned, lock_all, collapse_all}."""
    inp = {"devices": _devices()}
    body = _body()
    try:
        return _reply(store.mutate(lambda d: topology.set_layout(d, body, _known(inp, d))))
    except topology.TopologyError as e:
        return _fail(e)


@bp.route("/api/topology/arrange", methods=["POST"])
@guard
def api_arrange():
    """Auto-arrange: everything not locked (or one group's inside, {scope: gid})."""
    body = _body()
    scope = body.get("scope") or None
    if store.doc()["view"].get("lock_all"):
        return jsonify({"ok": False, "error": "The layout is locked — unlock it first"}), 409
    if scope and scope != topology.UNASSIGNED and scope not in store.doc()["groups"]:
        return jsonify({"ok": False, "error": "That group no longer exists"}), 404
    return jsonify({"ok": True, "graph": _graph_payload(force=True, scope=scope)})


# ---- links ---------------------------------------------------------------------------
@bp.route("/api/topology/links", methods=["POST"])
@guard
def api_link_create():
    inp = _inputs()
    try:
        return _reply(store.mutate(topology.create_link, _body(), _dev_map(inp), inp["radios"]))
    except topology.TopologyError as e:
        return _fail(e)


@bp.route("/api/topology/links/<lid>", methods=["POST", "DELETE"])
@guard
def api_link(lid):
    try:
        if request.method == "DELETE":
            return _reply(store.mutate(topology.delete_link, lid))
        inp = _inputs()
        return _reply(store.mutate(topology.update_link, lid, _body(), _dev_map(inp), inp["radios"]))
    except topology.TopologyError as e:
        return _fail(e)


# ---- suggestions -----------------------------------------------------------------------
def _current_suggestions(inp):
    with store.lock:
        d = store.doc()
        g = topology.build(d, inp["devices"], inp["registry"], inp["radios"], inp["switches"],
                           inp["routers"], inp["problems"], gateway=inp.get("gateway"),
                           scope="all" if request.args.get("scope") == "all" else "infra")
    return {s["id"]: s for s in g["suggestions"]}


@bp.route("/api/topology/suggestions/<sid>", methods=["POST"])
@guard
def api_suggestion(sid):
    """{action: accept | dismiss | restore | insert (shared port → new passive box)}"""
    body = _body()
    action = body.get("action")
    try:
        if action == "dismiss":
            return _reply(store.mutate(topology.dismiss, sid, True))
        if action == "restore":
            return _reply(store.mutate(topology.dismiss, sid, False))
        inp = _inputs()
        sug = _current_suggestions(inp).get(sid)
        if not sug:
            return jsonify({"ok": False, "error": "That suggestion is gone — the readings changed or it was already handled"}), 404
        devs = _dev_map(inp)
        if action == "accept":
            return _reply(store.mutate(topology.accept_suggestion, sug, devs, inp["radios"]))
        if action == "insert":
            return _reply(store.mutate(topology.insert_passive, sug, body, devs))
        return jsonify({"ok": False, "error": "Unknown action"}), 400
    except topology.TopologyError as e:
        return _fail(e)


@bp.route("/api/topology/suggestions/accept-all", methods=["POST"])
@guard
def api_suggestions_accept_all():
    """Confirm every proven connection at once ({medium?} narrows it)."""
    body = _body()
    inp = _inputs()
    sugs = [s for s in _current_suggestions(inp).values() if s.get("type") == "link"
            and (not body.get("medium") or s.get("medium") == body["medium"])]
    devs = _dev_map(inp)

    def run(d):
        done, failed = 0, []
        for s in sugs:
            try:
                topology.accept_suggestion(d, s, devs, inp["radios"])
                done += 1
            except topology.TopologyError as e:
                failed.append({"id": s["id"], "error": str(e)})
        return {"accepted": done, "failed": failed} if done else topology.NOCHANGE
    try:
        res = store.mutate(run) or {"accepted": 0, "failed": []}
        return _reply(res)
    except topology.TopologyError as e:
        return _fail(e)


@bp.route("/api/topology/dismissed/clear", methods=["POST"])
@guard
def api_dismissed_clear():
    def run(d):
        n = len(d["dismissed"])
        if not n:
            return topology.NOCHANGE
        d["dismissed"] = {}
        return {"restored": n}
    return _reply(store.mutate(run) or {"restored": 0})


# ---- icons ------------------------------------------------------------------------------
@bp.route("/api/topology/icons/<iid>")
def api_icon(iid):
    meta = store.doc()["icons"].get(iid)
    path = store.icon_path(iid)
    if not meta or not path:
        return jsonify({"ok": False, "error": "no such icon"}), 404
    try:
        resp = send_file(path, mimetype=meta.get("mime") or "application/octet-stream", max_age=86400)
    except OSError:
        return jsonify({"ok": False, "error": "no such icon"}), 404
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return resp


@bp.route("/api/topology/icons", methods=["POST"])
@guard
def api_icon_upload():
    """Multipart `file` or JSON {data: base64, name}; optional kind (every device of
    that type) or node (just that one)."""
    body = {}
    f = request.files.get("file")
    if f:
        data = f.read(topology.ICON_MAX_BYTES + 1)
        body = dict(request.form)
        name = f.filename or ""
    else:
        body = _body()
        try:
            data = base64.b64decode(str(body.get("data") or "").split(",")[-1], validate=False)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "That is not a picture"}), 400
        name = body.get("name") or ""
    inp = {"devices": _devices()}
    try:
        return _reply(store.mutate(lambda d: topology.add_icon(
            d, store, data, name, kind=body.get("kind") or None, node=body.get("node") or None,
            known=_known(inp, d))))
    except topology.TopologyError as e:
        return _fail(e)


@bp.route("/api/topology/icons/<iid>", methods=["DELETE"])
@guard
def api_icon_delete(iid):
    try:
        return _reply(store.mutate(topology.delete_icon, store, iid))
    except topology.TopologyError as e:
        return _fail(e)


@bp.route("/api/topology/type-icons", methods=["POST"])
@guard
def api_type_icon():
    body = _body()
    try:
        return _reply(store.mutate(topology.set_type_icon, str(body.get("kind") or ""), body.get("icon") or ""))
    except topology.TopologyError as e:
        return _fail(e)
