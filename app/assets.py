"""Per-category asset details for the device register.

A camera and a backhaul radio are not the same kind of asset: one needs lens,
mount height and NVR channel, the other needs dish size, bearing and mast
height. One flat form for both collects the wrong things for both, so the field
set is driven by the device's category — SHARED fields everything gets, plus a
group per category.

Values live in the device registry under `asset` (so they ride along in the
config export/backup and survive image updates). Only known field keys are
stored; the schema is the contract the UI renders from, so adding a field here
is all it takes to have it appear.
"""

# type: text (default) | date | number | select | textarea
SHARED = [
    {"k": "asset_tag", "label": "Asset tag"},
    {"k": "installed", "label": "Installed on", "type": "date"},
    {"k": "supplier", "label": "Supplier / installer"},
    {"k": "cost", "label": "Cost", "type": "text", "placeholder": "R"},
    {"k": "warranty_until", "label": "Warranty until", "type": "date"},
    {"k": "site_area", "label": "Area / building", "placeholder": "e.g. Kliphuis, workshop"},
    {"k": "power", "label": "Power source", "type": "select",
     "options": ["", "PoE switch", "PoE injector", "Mains adapter", "Solar / battery", "UPS"]},
    {"k": "asset_notes", "label": "Notes", "type": "textarea"},
]

# Radios and APs. Bearing/height/dish are what an installer needs to re-aim a
# link years later, and what makes a signal drop diagnosable.
_WIRELESS = [
    {"k": "radio_role", "label": "Role", "type": "select",
     "options": ["", "Access Point (PtMP)", "Station (PtMP)", "PtP master",
                 "PtP slave", "Client AP / hotspot", "Mesh"]},
    {"k": "antenna", "label": "Antenna / dish", "placeholder": "e.g. PBE-5AC-400, 25 dBi"},
    {"k": "azimuth", "label": "Bearing (°)", "type": "number", "placeholder": "compass heading"},
    {"k": "tilt", "label": "Down-tilt (°)", "type": "number"},
    {"k": "mast", "label": "Mast / pole", "placeholder": "e.g. 6 m pole at pump house"},
    {"k": "mast_height", "label": "Height above ground (m)", "type": "number"},
    {"k": "link_partner", "label": "Links to", "placeholder": "the radio at the other end"},
    {"k": "freq_plan", "label": "Planned frequency / width", "placeholder": "e.g. 5480 / 20 MHz"},
    {"k": "poe_injector", "label": "PoE injector / port"},
]

_SWITCHING = [
    {"k": "ports", "label": "Ports", "type": "number"},
    {"k": "poe_budget", "label": "PoE budget (W)", "type": "number"},
    {"k": "uplink", "label": "Uplink to"},
    {"k": "vlans", "label": "VLANs"},
    {"k": "mgmt_vlan", "label": "Management VLAN"},
]

_CAMERA = [
    {"k": "cam_type", "label": "Camera type", "type": "select",
     "options": ["", "Bullet", "Dome / turret", "PTZ", "Thermal", "ANPR / LPR", "Panoramic"]},
    {"k": "resolution", "label": "Resolution", "type": "select",
     "options": ["", "2 MP (1080p)", "4 MP", "5 MP", "6 MP", "8 MP (4K)", "12 MP"]},
    {"k": "lens_mm", "label": "Lens (mm)", "placeholder": "e.g. 2.8 or 2.8-12 varifocal"},
    {"k": "view", "label": "Points at", "placeholder": "what this camera watches"},
    {"k": "mount_height", "label": "Mount height (m)", "type": "number"},
    {"k": "ir_range", "label": "IR / illumination range (m)", "type": "number"},
    {"k": "poe_switch", "label": "Fed from switch"},
    {"k": "poe_port", "label": "Switch port"},
    {"k": "nvr_channel", "label": "NVR channel", "placeholder": "e.g. D3"},
    {"k": "rtsp_path", "label": "RTSP path"},
]

_RECORDER = [
    {"k": "channels", "label": "Channels", "type": "number"},
    {"k": "disks", "label": "Disks", "placeholder": "e.g. 2 × 4 TB Purple"},
    {"k": "retention_days", "label": "Recording retention (days)", "type": "number"},
    {"k": "cams_attached", "label": "Cameras recorded", "type": "number"},
    {"k": "remote_view", "label": "Remote viewing", "placeholder": "app / DDNS / P2P id"},
]

_POWER = [
    {"k": "panel_kw", "label": "Array size (kWp)", "type": "number"},
    {"k": "battery_kwh", "label": "Battery (kWh)", "type": "number"},
    {"k": "inverter", "label": "Inverter model"},
    {"k": "backs_up", "label": "Backs up", "placeholder": "what stays on when the grid drops"},
]

BY_CATEGORY = {
    "camera": _CAMERA,
    "nvr": _RECORDER,
    "network": _WIRELESS + _SWITCHING,
    "internet-ap": _WIRELESS + [{"k": "isp", "label": "ISP / package"},
                                {"k": "account", "label": "Account / line no."}],
    "router": _SWITCHING + [{"k": "isp", "label": "ISP / package"},
                            {"k": "wan_ip", "label": "WAN address / DDNS"}],
    "nas": [{"k": "disks", "label": "Disks"}, {"k": "raid", "label": "RAID level"},
            {"k": "capacity", "label": "Usable capacity"},
            {"k": "backup_of", "label": "Backs up"}],
    "solar": _POWER,
    "alarm": [{"k": "panel_model", "label": "Panel model"},
              {"k": "zones", "label": "Zones", "type": "number"},
              {"k": "armed_by", "label": "Armed by / keypads"},
              {"k": "monitoring", "label": "Monitoring company"},
              {"k": "radio_id", "label": "Radio / account id"}],
    "printer": [{"k": "toner", "label": "Toner / cartridge type"},
                {"k": "duplex", "label": "Duplex", "type": "select",
                 "options": ["", "Yes", "No"]},
                {"k": "queue", "label": "Print queue name"}],
    "voip": [{"k": "extension", "label": "Extension"},
             {"k": "pbx", "label": "PBX / provider"},
             {"k": "did", "label": "DID / number"}],
    "server": [{"k": "role", "label": "Role / services"}, {"k": "os", "label": "OS"},
               {"k": "cpu_ram", "label": "CPU / RAM"},
               {"k": "backup", "label": "Backed up to"}],
    "pc": [{"k": "user", "label": "Used by"}, {"k": "os", "label": "OS"},
           {"k": "backup", "label": "Backed up to"}],
    "iot": [{"k": "controls", "label": "Controls / measures"},
            {"k": "app", "label": "App / hub"}],
    "media": [{"k": "screen", "label": "Screen size"}, {"k": "feeds", "label": "Shows"}],
}


def schema(category):
    """Field list for a category: its own group first, then the shared ones."""
    return {"category": category or "unknown",
            "fields": BY_CATEGORY.get(category or "", []) + SHARED}


def all_schemas():
    return {"shared": SHARED, "by_category": BY_CATEGORY}


def clean(category, values):
    """Keep only fields this category defines, drop blanks. Values are stored as
    given (free text) — a farm's paperwork rarely fits a strict type."""
    allowed = {f["k"] for f in schema(category)["fields"]}
    out = {}
    for k, v in (values or {}).items():
        if k in allowed:
            v = v.strip() if isinstance(v, str) else v
            if v not in ("", None):
                out[k] = v
    return out


def completeness(category, values):
    """How much of the register is filled in — drives the 'not completed' nudge."""
    fields = schema(category)["fields"]
    filled = sum(1 for f in fields if (values or {}).get(f["k"]) not in ("", None))
    return {"filled": filled, "total": len(fields),
            "pct": round(100 * filled / len(fields)) if fields else 0}
