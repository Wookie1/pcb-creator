"""Incremental circuit builder — many small validated calls instead of one
giant netlist JSON.

A draft lives in <project>/<project>_circuit_draft.json and is mutated by
add_component / connect_pins / etc. finalize() compiles the draft into the
canonical circuit_schema netlist (same shape the KiCad import produces), so
the rest of the pipeline (placement → routing → DRC → export) is unchanged.

All functions return plain dicts {"ok": bool, ...}; the MCP layer wraps them
in the response envelope.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from validators.net_classes import infer_net_class, infer_electrical_type
from validators.pinout import parse_pinout, expected_pin_count

DESIGNATOR_RE = re.compile(r"^[A-Z]{1,3}[0-9]+$")
PROJECT_RE = re.compile(r"^[a-z][a-z0-9_]*$")

COMPONENT_TYPES = [
    "resistor", "capacitor", "inductor", "led", "diode",
    "transistor_npn", "transistor_pnp", "transistor_nmos", "transistor_pmos",
    "ic", "connector", "switch", "voltage_regulator", "crystal", "fuse",
    "relay",
]

# Default pin counts when neither pinout nor package patterns resolve one.
_DEFAULT_PIN_COUNT = {
    "resistor": 2, "capacitor": 2, "inductor": 2, "led": 2, "diode": 2,
    "crystal": 2, "fuse": 2,
    "transistor_npn": 3, "transistor_pnp": 3,
    "transistor_nmos": 3, "transistor_pmos": 3,
}

# Conventional pin names usable in connect_pins (e.g. "D1.anode").
# Transistor numbering follows the SOT-23 convention (the dominant SMD
# package); pass an explicit pinout to add_component to override (e.g. for
# TO-92 parts with different lead order).
_DEFAULT_PIN_NAMES = {
    "led": {1: "anode", 2: "cathode"},
    "diode": {1: "anode", 2: "cathode"},
    "transistor_npn": {1: "base", 2: "emitter", 3: "collector"},
    "transistor_pnp": {1: "base", 2: "emitter", 3: "collector"},
    "transistor_nmos": {1: "gate", 2: "source", 3: "drain"},
    "transistor_pmos": {1: "gate", 2: "source", 3: "drain"},
    # 78xx linear regulator convention (LM1117-style parts differ — pass an
    # explicit pinout for those)
    "voltage_regulator": {1: "IN", 2: "GND", 3: "OUT"},
}

_PIN_TOKEN_RE = re.compile(r"^([A-Z]{1,3}[0-9]+)\.(.+)$")


def _draft_path(project_dir: Path, project_name: str) -> Path:
    return project_dir / f"{project_name}_circuit_draft.json"


def _netlist_path(project_dir: Path, project_name: str) -> Path:
    return project_dir / f"{project_name}_netlist.json"


def load_draft(project_dir: Path, project_name: str) -> dict | None:
    path = _draft_path(project_dir, project_name)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _save_draft(project_dir: Path, project_name: str, draft: dict) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    _draft_path(project_dir, project_name).write_text(
        json.dumps(draft, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def create_draft(project_dir: Path, project_name: str, description: str,
                 board_width_mm: float, board_height_mm: float,
                 layers: int = 2) -> dict:
    """Create a new empty circuit draft."""
    if not PROJECT_RE.match(project_name):
        return {"ok": False, "code": "bad_project_name",
                "error": f"Invalid project_name '{project_name}'. Use lowercase "
                         "letters, digits, and underscores (start with a letter)."}
    if load_draft(project_dir, project_name) is not None:
        return {"ok": False, "code": "draft_exists",
                "error": f"A circuit draft already exists for '{project_name}'. "
                         "Continue with add_component/connect_pins, inspect it "
                         "with list_circuit, or pick a new project name."}
    if _netlist_path(project_dir, project_name).exists():
        return {"ok": False, "code": "netlist_exists",
                "error": f"Project '{project_name}' already has a netlist "
                         "(e.g. from a KiCad import). Pick a new project name."}
    if layers not in (2, 4):
        return {"ok": False, "code": "bad_layers",
                "error": f"layers must be 2 or 4, got {layers}."}
    try:
        w, h = float(board_width_mm), float(board_height_mm)
    except (TypeError, ValueError):
        return {"ok": False, "code": "bad_board",
                "error": "board_width_mm and board_height_mm must be numbers (mm)."}
    if w < 5 or h < 5 or w > 500 or h > 500:
        return {"ok": False, "code": "bad_board",
                "error": f"Board {w}x{h}mm is outside the sane range (5-500mm per side)."}

    draft = {
        "version": "1.0",
        "project_name": project_name,
        "description": description or "",
        "board": {"width_mm": w, "height_mm": h, "layers": layers},
        "components": {},
        "nets": {},
        "no_connect": [],
    }
    _save_draft(project_dir, project_name, draft)
    return {"ok": True, "project_name": project_name,
            "board": draft["board"]}


def add_component(project_dir: Path, project_name: str, designator: str,
                  component_type: str, value: str, package: str,
                  pinout: str | None = None, pin_count: int | None = None,
                  footprint_lookup=None) -> dict:
    """Add one component to the draft. Returns its resolved pin table.

    pin_count: authoritative override when given (else derived from pinout,
        the package name, the footprint, or the component type).
    footprint_lookup: callable(package, pin_count) -> FootprintDef | None
        (injected so the MCP layer can pass the configured tiered lookup).
    """
    draft = load_draft(project_dir, project_name)
    if draft is None:
        return {"ok": False, "code": "no_draft",
                "error": f"No circuit draft for '{project_name}'. "
                         "Call create_circuit first."}
    if not DESIGNATOR_RE.match(designator or ""):
        return {"ok": False, "code": "bad_designator",
                "error": f"Invalid designator '{designator}'. Use 1-3 uppercase "
                         "letters followed by a number, e.g. R1, C2, U1, J1."}
    if designator in draft["components"]:
        return {"ok": False, "code": "duplicate_designator",
                "error": f"Component '{designator}' already exists. Use a new "
                         "designator, or remove_component first to replace it."}
    if component_type not in COMPONENT_TYPES:
        return {"ok": False, "code": "bad_type",
                "error": f"Unknown component_type '{component_type}'. "
                         f"Valid types: {', '.join(COMPONENT_TYPES)}."}
    if not value:
        return {"ok": False, "code": "bad_value",
                "error": "value must be a non-empty string, e.g. '330ohm', "
                         "'100nF', 'red', 'NE555'."}
    if not package:
        return {"ok": False, "code": "bad_package",
                "error": "package must be a non-empty string, e.g. '0805', "
                         "'DIP-8', 'SOT-23'."}

    # Resolve pin names/types from an explicit pinout string when given.
    pin_names: dict[int, str] = {}
    pin_alts: dict[int, list[str]] = {}
    pin_types: dict[int, str] = {}
    if pinout:
        parsed = parse_pinout(pinout)
        if not parsed:
            return {"ok": False, "code": "bad_pinout",
                    "error": "pinout did not parse. Format: "
                             "'1:GND 2:TRIG 3:OUT ... 8:VCC' "
                             "(pin_number:name, whitespace-separated)."}
        pin_names = {n: p.primary_name for n, p in parsed.items()}
        pin_alts = {n: list(p.alt_names) for n, p in parsed.items()
                    if p.alt_names}
        pin_types = {n: p.inferred_electrical_type for n, p in parsed.items()
                     if p.inferred_electrical_type}

    # Resolve pin count: explicit arg > pinout > package pattern > footprint
    # > type default.
    if pin_count is not None:
        try:
            pin_count = int(pin_count)
        except (TypeError, ValueError):
            return {"ok": False, "code": "bad_pin_count",
                    "error": "pin_count must be an integer."}
        if pin_count < 1 or pin_count > 1000:
            return {"ok": False, "code": "bad_pin_count",
                    "error": f"pin_count {pin_count} is out of range (1-1000)."}
    if pin_count is None:
        pin_count = max(pin_names) if pin_names else None
    if pin_count is None:
        pin_count = expected_pin_count(package)
    fp = footprint_lookup(package, pin_count or 0) if footprint_lookup else None
    if pin_count is None and fp is not None:
        pin_count = len(fp.pin_offsets)
    if pin_count is None:
        pin_count = _DEFAULT_PIN_COUNT.get(component_type)
    if pin_count is None:
        return {"ok": False, "code": "unknown_pin_count",
                "error": f"Cannot determine the pin count of '{package}' for "
                         f"{designator}. Re-call add_component with a pinout "
                         "string, e.g. pinout='1:GND 2:TRIG 3:OUT 4:RESET "
                         "5:CTRL 6:THRES 7:DISCH 8:VCC'."}

    # Footprint gate at add time — fail fast, not at placement.
    if fp is None and footprint_lookup is not None:
        fp = footprint_lookup(package, pin_count)
    footprint_resolved = fp is not None
    if not footprint_resolved:
        return {"ok": False, "code": "unresolved_footprint",
                "package": package, "pin_count": pin_count,
                "error": f"Package '{package}' does not resolve to a footprint "
                         "(tried KiCad library, IPC-7351, cache, built-in). "
                         "Call provide_footprint to supply geometry, or use a "
                         "recognized package name, then re-run add_component."}

    # Conventional names for parts without an explicit pinout.
    if not pin_names and component_type in _DEFAULT_PIN_NAMES:
        pin_names = dict(_DEFAULT_PIN_NAMES[component_type])

    comp = {
        "component_type": component_type,
        "value": value,
        "package": package,
        "pin_count": pin_count,
        "pin_names": {str(k): v for k, v in pin_names.items()},
        "pin_alts": {str(k): v for k, v in pin_alts.items()},
        "pin_types": {str(k): v for k, v in pin_types.items()},
    }
    draft["components"][designator] = comp
    _save_draft(project_dir, project_name, draft)

    pins = []
    for n in range(1, pin_count + 1):
        entry = {"pin": n}
        name = pin_names.get(n)
        if name:
            entry["name"] = name
        pins.append(entry)
    return {"ok": True, "designator": designator, "package": package,
            "pin_count": pin_count, "pins": pins,
            "component_count": len(draft["components"])}


def remove_component(project_dir: Path, project_name: str,
                     designator: str) -> dict:
    draft = load_draft(project_dir, project_name)
    if draft is None:
        return {"ok": False, "code": "no_draft",
                "error": f"No circuit draft for '{project_name}'."}
    if designator not in draft["components"]:
        return {"ok": False, "code": "unknown_designator",
                "error": f"No component '{designator}'. Existing: "
                         f"{', '.join(sorted(draft['components'])) or '(none)'}."}
    del draft["components"][designator]
    prefix = f"{designator}."
    removed_from = []
    for net_name, net in list(draft["nets"].items()):
        before = len(net["pins"])
        net["pins"] = [p for p in net["pins"] if not p.startswith(prefix)]
        if len(net["pins"]) != before:
            removed_from.append(net_name)
        if not net["pins"]:
            del draft["nets"][net_name]
    draft["no_connect"] = [p for p in draft["no_connect"]
                           if not p.startswith(prefix)]
    _save_draft(project_dir, project_name, draft)
    return {"ok": True, "designator": designator,
            "removed_from_nets": removed_from,
            "component_count": len(draft["components"])}


def _resolve_pin_token(draft: dict, token: str) -> tuple[str | None, str | None]:
    """Resolve 'U1.7' or 'D1.anode' → ('U1.7' canonical, None) or (None, error).

    Name matching order: exact primary name → exact alternate name → unique
    prefix of either (so 'IN' finds 'INPUT', 'GND' finds 'GND/ADJ'). All
    case-insensitive.
    """
    m = _PIN_TOKEN_RE.match(token.strip())
    if not m:
        return None, (f"Pin '{token}' is malformed. Use DESIGNATOR.PIN, e.g. "
                      "'U1.7' or 'D1.anode'.")
    des, pin = m.group(1), m.group(2)
    comp = draft["components"].get(des)
    if comp is None:
        known = ", ".join(sorted(draft["components"])) or "(none added yet)"
        return None, (f"Unknown component '{des}' in '{token}'. "
                      f"Known designators: {known}.")
    pin_count = comp["pin_count"]
    if pin.isdigit():
        n = int(pin)
        if not (1 <= n <= pin_count):
            return None, (f"{des} has pins 1-{pin_count}; '{token}' is out of "
                          "range.")
        return f"{des}.{n}", None

    want = pin.lower()
    # name -> pin number, primaries first so they win ties over alts
    name_map: list[tuple[str, int]] = []
    for k, v in comp.get("pin_names", {}).items():
        name_map.append((v.lower(), int(k)))
    for k, alts in comp.get("pin_alts", {}).items():
        for a in alts:
            name_map.append((a.lower(), int(k)))

    exact = [n for nm, n in name_map if nm == want]
    if exact:
        return f"{des}.{exact[0]}", None
    # Ordinal convention for duplicate pin names: 'VCC2' = the 2nd pin named
    # VCC (common for ICs with multiple VCC/GND pins).
    om = re.match(r"^(.*?)(\d+)$", want)
    if om and om.group(1):
        base, ordinal = om.group(1), int(om.group(2))
        dupes = sorted({n for nm, n in name_map if nm == base})
        if len(dupes) >= ordinal >= 1:
            return f"{des}.{dupes[ordinal - 1]}", None
    prefix = sorted({n for nm, n in name_map if nm.startswith(want)})
    if len(prefix) == 1:
        return f"{des}.{prefix[0]}", None
    if len(prefix) > 1:
        return None, (f"{des} pin name '{pin}' is ambiguous — it matches pins "
                      f"{prefix}. Use the full name or the pin number.")
    valid = [f"{k}:{v}" for k, v in sorted(
        comp.get("pin_names", {}).items(), key=lambda kv: int(kv[0]))]
    return None, (f"{des} has no pin named '{pin}'. Valid pins: 1-{pin_count}"
                  + (f" (named: {', '.join(valid)})" if valid else "")
                  + ". Use a pin number or a listed name.")


def _find_pin_net(draft: dict, canonical: str) -> str | None:
    for net_name, net in draft["nets"].items():
        if canonical in net["pins"]:
            return net_name
    return None


def connect_pins(project_dir: Path, project_name: str, net_name: str,
                 pins: list[str], net_class: str | None = None) -> dict:
    """Connect pins to a named net (creating it if new). Idempotent."""
    draft = load_draft(project_dir, project_name)
    if draft is None:
        return {"ok": False, "code": "no_draft",
                "error": f"No circuit draft for '{project_name}'. "
                         "Call create_circuit first."}
    if not net_name or not net_name.strip():
        return {"ok": False, "code": "bad_net_name",
                "error": "net_name must be non-empty, e.g. 'VCC', 'GND', "
                         "'LED_DRIVE'."}
    net_name = net_name.strip()
    if net_class is not None and net_class not in ("signal", "power", "ground"):
        return {"ok": False, "code": "bad_net_class",
                "error": f"net_class '{net_class}' invalid. Use 'signal', "
                         "'power', or 'ground' (or omit to auto-infer)."}
    if not pins:
        return {"ok": False, "code": "no_pins",
                "error": "pins must be a non-empty list, e.g. ['U1.8', 'C1.1']."}

    canonical: list[str] = []
    for token in pins:
        c, err = _resolve_pin_token(draft, token)
        if err:
            return {"ok": False, "code": "bad_pin", "error": err}
        canonical.append(c)

    # Conflicts: a pin already on a DIFFERENT net is an error (idempotent for
    # the same net).
    conflicts = []
    for c in canonical:
        existing = _find_pin_net(draft, c)
        if existing and existing != net_name:
            conflicts.append((c, existing))
    if conflicts:
        c, existing = conflicts[0]
        return {"ok": False, "code": "pin_conflict",
                "pin": c, "existing_net": existing,
                "error": f"Pin {c} is already connected to net '{existing}'. "
                         f"Call disconnect_pins('{project_name}', "
                         f"'{existing}', ['{c}']) first if you meant to move it."}

    net = draft["nets"].get(net_name)
    if net is None:
        net = {"net_class": net_class or infer_net_class(net_name), "pins": []}
        draft["nets"][net_name] = net
    elif net_class is not None:
        net["net_class"] = net_class

    added = []
    for c in canonical:
        if c not in net["pins"]:
            net["pins"].append(c)
            added.append(c)
        if c in draft["no_connect"]:
            draft["no_connect"].remove(c)
    _save_draft(project_dir, project_name, draft)
    return {"ok": True, "net_name": net_name,
            "net_class": net["net_class"],
            "added": added, "already_connected": [c for c in canonical
                                                  if c not in added],
            "net_pins": list(net["pins"])}


def disconnect_pins(project_dir: Path, project_name: str, net_name: str,
                    pins: list[str]) -> dict:
    draft = load_draft(project_dir, project_name)
    if draft is None:
        return {"ok": False, "code": "no_draft",
                "error": f"No circuit draft for '{project_name}'."}
    net = draft["nets"].get(net_name)
    if net is None:
        known = ", ".join(sorted(draft["nets"])) or "(none)"
        return {"ok": False, "code": "unknown_net",
                "error": f"No net '{net_name}'. Existing nets: {known}."}
    removed = []
    for token in pins:
        c, err = _resolve_pin_token(draft, token)
        if err:
            return {"ok": False, "code": "bad_pin", "error": err}
        if c in net["pins"]:
            net["pins"].remove(c)
            removed.append(c)
    if not net["pins"]:
        del draft["nets"][net_name]
    _save_draft(project_dir, project_name, draft)
    return {"ok": True, "net_name": net_name, "removed": removed,
            "net_deleted": net_name not in draft["nets"]}


def mark_no_connect(project_dir: Path, project_name: str,
                    pins: list[str]) -> dict:
    """Mark pins as intentionally unconnected (finalize requires every pin to
    be either on a net or explicitly no-connect)."""
    draft = load_draft(project_dir, project_name)
    if draft is None:
        return {"ok": False, "code": "no_draft",
                "error": f"No circuit draft for '{project_name}'."}
    marked = []
    for token in pins:
        c, err = _resolve_pin_token(draft, token)
        if err:
            return {"ok": False, "code": "bad_pin", "error": err}
        existing = _find_pin_net(draft, c)
        if existing:
            return {"ok": False, "code": "pin_connected",
                    "error": f"Pin {c} is connected to net '{existing}' — "
                             "disconnect it first if it is truly unused."}
        if c not in draft["no_connect"]:
            draft["no_connect"].append(c)
            marked.append(c)
    _save_draft(project_dir, project_name, draft)
    return {"ok": True, "marked": marked,
            "no_connect": list(draft["no_connect"])}


def _unconnected_pins(draft: dict) -> list[str]:
    connected = {p for net in draft["nets"].values() for p in net["pins"]}
    nc = set(draft["no_connect"])
    out = []
    for des, comp in draft["components"].items():
        for n in range(1, comp["pin_count"] + 1):
            c = f"{des}.{n}"
            if c not in connected and c not in nc:
                out.append(c)
    return out


def list_circuit(draft: dict) -> dict:
    components = []
    for des in sorted(draft["components"]):
        comp = draft["components"][des]
        components.append({
            "designator": des,
            "component_type": comp["component_type"],
            "value": comp["value"],
            "package": comp["package"],
            "pin_count": comp["pin_count"],
        })
    nets = []
    for name in sorted(draft["nets"]):
        net = draft["nets"][name]
        nets.append({"net_name": name, "net_class": net["net_class"],
                     "pins": list(net["pins"])})
    return {
        "ok": True,
        "project_name": draft["project_name"],
        "board": draft["board"],
        "components": components,
        "nets": nets,
        "no_connect": list(draft["no_connect"]),
        "unconnected_pins": _unconnected_pins(draft),
    }


# ---------------------------------------------------------------------------
# Finalize: compile draft → canonical netlist
# ---------------------------------------------------------------------------

def _comp_id(designator: str) -> str:
    return "comp_" + designator.lower()


def _port_id(designator: str, pin: int) -> str:
    return f"port_{designator.lower()}_{pin}"


def _net_id(name: str, seen: set[str]) -> str:
    safe = re.sub(r"[^a-z0-9]", "_", name.lower()).strip("_") or "unnamed"
    if safe[0].isdigit():
        safe = "n" + safe
    base = "net_" + safe
    candidate, counter = base, 2
    while candidate in seen:
        candidate = f"{base}_{counter}"
        counter += 1
    seen.add(candidate)
    return candidate


def finalize(project_dir: Path, project_name: str) -> dict:
    """Compile the draft into <project>_netlist.json and validate it."""
    draft = load_draft(project_dir, project_name)
    if draft is None:
        return {"ok": False, "code": "no_draft",
                "error": f"No circuit draft for '{project_name}'. "
                         "Call create_circuit first."}
    if not draft["components"]:
        return {"ok": False, "code": "empty",
                "error": "The circuit has no components. Call add_component."}

    # Every pin must be on a net or explicitly no-connect.
    unconnected = _unconnected_pins(draft)
    if unconnected:
        return {"ok": False, "code": "unconnected_pins",
                "unconnected_pins": unconnected,
                "error": f"{len(unconnected)} pin(s) are neither connected nor "
                         f"marked no-connect: {', '.join(unconnected[:12])}"
                         + ("..." if len(unconnected) > 12 else "") + ". "
                         "Connect them with connect_pins or mark them with "
                         "mark_no_connect."}

    # Nets need at least 2 pins.
    short_nets = [n for n, net in draft["nets"].items()
                  if len(net["pins"]) < 2]
    if short_nets:
        return {"ok": False, "code": "single_pin_nets",
                "nets": short_nets,
                "error": f"Net(s) with fewer than 2 pins: "
                         f"{', '.join(short_nets)}. Add more pins with "
                         "connect_pins, or disconnect_pins to drop the net."}

    elements: list[dict] = []
    pin_net_class: dict[str, str] = {}
    for net_name, net in draft["nets"].items():
        for p in net["pins"]:
            pin_net_class[p] = net["net_class"]

    for des in sorted(draft["components"]):
        comp = draft["components"][des]
        cid = _comp_id(des)
        elements.append({
            "element_type": "component",
            "component_id": cid,
            "designator": des,
            "component_type": comp["component_type"],
            "value": comp["value"],
            "package": comp["package"],
        })
        for n in range(1, comp["pin_count"] + 1):
            canonical = f"{des}.{n}"
            name = comp.get("pin_names", {}).get(str(n)) or str(n)
            if canonical in draft["no_connect"]:
                etype = "no_connect"
            else:
                etype = comp.get("pin_types", {}).get(str(n))
                nclass = pin_net_class.get(canonical, "signal")
                # The explicit pinout type wins unless the net contradicts it
                # at the power/ground level.
                inferred = infer_electrical_type(nclass, comp["component_type"])
                if not etype or nclass in ("power", "ground"):
                    etype = inferred
            elements.append({
                "element_type": "port",
                "port_id": _port_id(des, n),
                "component_id": cid,
                "pin_number": n,
                "name": name,
                "electrical_type": etype,
            })

    seen_net_ids: set[str] = set()
    for net_name in sorted(draft["nets"]):
        net = draft["nets"][net_name]
        port_ids = []
        for p in net["pins"]:
            des, pin = p.split(".")
            port_ids.append(_port_id(des, int(pin)))
        elements.append({
            "element_type": "net",
            "net_id": _net_id(net_name, seen_net_ids),
            "name": net_name,
            "connected_port_ids": port_ids,
            "net_class": net["net_class"],
        })

    netlist = {
        "version": "1.0",
        "project_name": project_name,
        "description": draft.get("description", ""),
        "elements": elements,
    }

    netlist_path = _netlist_path(project_dir, project_name)
    netlist_path.write_text(json.dumps(netlist, indent=2), encoding="utf-8")

    # Full validation: schema + referential integrity + electrical DRC.
    from validators.validate_netlist import validate_netlist
    result = validate_netlist(str(netlist_path))

    # Footprint gate (should be clean — add_component gates — but re-check).
    from validators.verify_footprints import verify_footprints
    unresolved = verify_footprints(netlist)

    if not result["valid"] or unresolved:
        return {"ok": False, "code": "validation_failed",
                "netlist_path": str(netlist_path),
                "errors": result.get("errors", []),
                "warnings": result.get("warnings", []),
                "unresolved_footprints": unresolved,
                "error": "The compiled netlist failed validation — see "
                         "'errors'. Fix with add_component/connect_pins/"
                         "remove_component and re-run finalize_circuit."}

    return {"ok": True,
            "netlist_path": str(netlist_path),
            "component_count": len(draft["components"]),
            "net_count": len(draft["nets"]),
            "warnings": result.get("warnings", []),
            "board": draft["board"]}
