"""Convert a KiCad netlist (.net) or schematic (.kicad_sch) to PCB Creator
circuit_schema.json so a mid-stream KiCad project can continue inside
pcb-creator without starting over.

Accepted inputs
---------------
*.net       KiCad netlist export (File → Export → Netlist in KiCad schematic
            editor).  Carries full connectivity + component metadata.  This is
            the primary and most reliable input.

*.kicad_sch KiCad schematic file.  pcb-creator will automatically look for a
            sibling *.net file with the same stem and use it for connectivity.
            If no sibling .net exists, the caller is told to export one first.

Public API
----------
    from exporters.kicad_netlist_importer import convert_kicad_netlist
    result = convert_kicad_netlist("path/to/board.net", project_name="my_board")
    # result["netlist"]   -> circuit_schema dict (write as *_netlist.json)
    # result["warnings"]  -> list[str] of non-fatal issues
"""

from __future__ import annotations

import re
from pathlib import Path

from .kicad_importer import (
    parse_kicad_sexpr as _parse,
    _find_field as _find,
    _find_all,
)


# ---------------------------------------------------------------------------
# Component-type inference from reference-designator prefix
# ---------------------------------------------------------------------------

_REF_TYPE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^C\d"),   "capacitor"),
    (re.compile(r"^R\d"),   "resistor"),
    (re.compile(r"^L\d"),   "inductor"),
    (re.compile(r"^LED\d"), "led"),
    (re.compile(r"^D\d"),   "diode"),
    (re.compile(r"^Q\d"),   "transistor_npn"),
    (re.compile(r"^U\d"),   "ic"),
    (re.compile(r"^IC\d"),  "ic"),
    (re.compile(r"^Y\d"),   "crystal"),
    (re.compile(r"^X\d"),   "crystal"),
    # Connectors come in many designator flavours. SWD/HDR/TB must precede the
    # bare H/SW rules below (SWD is a connector, not a switch; HDR is a header,
    # not a mounting hole). J/P/CN are the classic KiCad connector prefixes.
    (re.compile(r"^SWD"),   "connector"),
    (re.compile(r"^HDR"),   "connector"),
    (re.compile(r"^TB\d"),  "connector"),
    (re.compile(r"^J\d"),   "connector"),
    (re.compile(r"^P\d"),   "connector"),
    (re.compile(r"^CN\d"),  "connector"),
    (re.compile(r"^CON\d"), "connector"),
    (re.compile(r"^SW\d"),  "switch"),
    (re.compile(r"^K\d"),   "relay"),
    (re.compile(r"^F\d"),   "fuse"),
]

# Footprint/package name keywords → component type. Checked BEFORE the
# designator prefix because the footprint is a far more reliable signal: a part
# designated "TB3" or "SWD1" or an unconventional prefix is still unambiguously
# a connector if its footprint is a TerminalBlock / Connector / FFC pattern.
# (morgan_carrier_v14 had TB1-5/HDR1/SWD1 all mis-classified as "ic" because
# their designators weren't in the prefix table, so the optimizer relocated
# them off the board edge.)
_PKG_TYPE_KEYWORDS: list[tuple[str, str]] = [
    ("terminalblock", "connector"),
    ("terminal_block", "connector"),
    ("connector", "connector"),
    ("pinheader", "connector"),
    ("pin_header", "connector"),
    ("pinsocket", "connector"),
    ("idc", "connector"),
    ("molex", "connector"),
    ("jst", "connector"),
    ("phoenix", "connector"),
    ("ffc", "connector"),
    ("fpc", "connector"),
    ("fh35", "connector"),       # Hirose FH35 FFC/FPC connector
]

_VALID_COMPONENT_TYPES = {
    "resistor", "capacitor", "inductor", "led", "diode",
    "transistor_npn", "transistor_pnp", "transistor_nmos", "transistor_pmos",
    "ic", "connector", "switch", "voltage_regulator", "crystal", "fuse", "relay",
}


def _infer_component_type(ref: str, package: str = "") -> str:
    # Footprint keywords win over the designator prefix (more reliable).
    if package:
        pkg_l = package.lower()
        for kw, ctype in _PKG_TYPE_KEYWORDS:
            if kw in pkg_l:
                return ctype
    for pattern, ctype in _REF_TYPE_RULES:
        if pattern.match(ref):
            return ctype
    return "ic"  # safe fallback for unknown prefixes (MCUs, FPGAs, modules, …)


# ---------------------------------------------------------------------------
# Net-class inference from net name
# ---------------------------------------------------------------------------

from validators.net_classes import (
    infer_net_class as _infer_net_class,
    infer_electrical_type as _infer_electrical_type,
)


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

def _comp_id(ref: str) -> str:
    return "comp_" + re.sub(r"[^a-z0-9]", "_", ref.lower())


def _port_id(ref: str, pin: str | int) -> str:
    safe_pin = re.sub(r"[^a-z0-9]", "_", str(pin).lower())
    return f"port_{re.sub(r'[^a-z0-9]', '_', ref.lower())}_{safe_pin}"


def _net_id(name: str, seen: set[str]) -> str:
    """Generate a unique net_id from a KiCad net name."""
    # strip leading slash (KiCad hierarchical net prefix)
    clean = name.lstrip("/").strip()
    safe = re.sub(r"[^a-z0-9]", "_", clean.lower()).strip("_") or "unnamed"
    # ensure starts with a letter/digit per schema pattern
    if safe[0].isdigit():
        safe = "n" + safe
    base = "net_" + safe
    candidate = base
    counter = 2
    while candidate in seen:
        candidate = f"{base}_{counter}"
        counter += 1
    seen.add(candidate)
    return candidate


def _strip_footprint_library(fp: str) -> str:
    """'Resistor_SMD:R_0805_2012Metric' → 'R_0805_2012Metric'."""
    return fp.split(":")[-1] if ":" in fp else fp


# ---------------------------------------------------------------------------
# KiCad .net parser
# ---------------------------------------------------------------------------

def _parse_dot_net(text: str) -> tuple[list[dict], list[dict]]:
    """Parse KiCad .net S-expression.

    Returns:
        components: list of {ref, value, footprint}
        nets:       list of {name, nodes: [{ref, pin}]}
    """
    tree = _parse(text)
    # tree = [["export", ...]]
    root = tree[0] if tree and isinstance(tree[0], list) else tree

    # --- components ---
    components: list[dict] = []
    comps_node = _find(root, "components")
    if comps_node:
        for comp in _find_all(comps_node, "comp"):
            ref_node = _find(comp, "ref")
            val_node = _find(comp, "value")
            fp_node  = _find(comp, "footprint")
            ref = ref_node[1] if ref_node and len(ref_node) > 1 else ""
            # skip KiCad internal power symbols (#PWR*, #FLG*)
            if not ref or ref.startswith("#"):
                continue
            components.append({
                "ref":       ref,
                "value":     val_node[1]  if val_node  and len(val_node)  > 1 else ref,
                "footprint": fp_node[1]   if fp_node   and len(fp_node)   > 1 else "",
            })

    # --- nets ---
    nets: list[dict] = []
    nets_node = _find(root, "nets")
    if nets_node:
        for net in _find_all(nets_node, "net"):
            name_node = _find(net, "name")
            name = name_node[1] if name_node and len(name_node) > 1 else "unnamed"
            nodes = []
            for node in _find_all(net, "node"):
                r = _find(node, "ref")
                p = _find(node, "pin")
                if r and p and len(r) > 1 and len(p) > 1:
                    nodes.append({"ref": r[1], "pin": p[1]})
            if nodes:
                nets.append({"name": name, "nodes": nodes})

    return components, nets


# ---------------------------------------------------------------------------
# KiCad .kicad_sch parser (component metadata only — no connectivity)
# ---------------------------------------------------------------------------

def _parse_kicad_sch_components(text: str) -> list[dict]:
    """Extract component metadata from a .kicad_sch file.

    Returns list of {ref, value, footprint}.  Connectivity is NOT available
    from the schematic file alone; the caller must use the sibling .net file.
    """
    tree = _parse(text)
    root = tree[0] if tree and isinstance(tree[0], list) else tree

    components: list[dict] = []
    for symbol in _find_all(root, "symbol"):
        props: dict[str, str] = {}
        for prop in _find_all(symbol, "property"):
            if len(prop) >= 3:
                props[prop[1]] = prop[2]
        ref = props.get("Reference", "")
        # skip KiCad internal power/flag symbols
        if not ref or ref.startswith("#"):
            continue
        components.append({
            "ref":       ref,
            "value":     props.get("Value", ref),
            "footprint": props.get("Footprint", ""),
        })

    # Deduplicate (multi-unit symbols appear once per unit)
    seen: set[str] = set()
    unique: list[dict] = []
    for c in components:
        if c["ref"] not in seen:
            seen.add(c["ref"])
            unique.append(c)
    return unique


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------

def _build_netlist(
    project_name: str,
    components: list[dict],
    nets: list[dict],
    description: str = "",
) -> tuple[dict, list[str]]:
    """Convert parsed component + net lists into a circuit_schema.json dict.

    Returns (netlist_dict, warnings).
    """
    warnings: list[str] = []

    # Build a lookup: ref -> component metadata
    comp_meta: dict[str, dict] = {c["ref"]: c for c in components}

    # Collect all (ref, pin) pairs that appear in nets so we can create ports
    # for only the connected pins (unconnected pins are irrelevant for routing).
    connected_pins: dict[str, set[str]] = {}  # ref -> {pin, ...}
    for net in nets:
        for node in net["nodes"]:
            ref, pin = node["ref"], str(node["pin"])
            if ref not in comp_meta:
                # Pin belongs to a component not listed in components section
                # (can happen if .net has partial info); create a stub.
                warnings.append(
                    f"Component '{ref}' appears in nets but not in components list; "
                    f"creating stub entry."
                )
                comp_meta[ref] = {"ref": ref, "value": ref, "footprint": ""}
            connected_pins.setdefault(ref, set()).add(pin)

    elements: list[dict] = []
    net_id_seen: set[str] = set()

    # --- component + port elements ---
    for ref, meta in sorted(comp_meta.items()):
        pkg_raw  = meta.get("footprint", "")
        pkg      = _strip_footprint_library(pkg_raw) if pkg_raw else "Unknown"
        ctype    = _infer_component_type(ref, pkg)
        value    = meta.get("value", ref) or ref

        elements.append({
            "element_type":  "component",
            "component_id":  _comp_id(ref),
            "designator":    ref,
            "component_type": ctype,
            "value":         value,
            "package":       pkg,
            "description":   f"{ctype} {value}",
            "properties":    {},
        })

        for pin in sorted(connected_pins.get(ref, ()), key=lambda p: (len(p), p)):
            # pin number: use integer if possible, else string index
            try:
                pin_int = int(pin)
            except ValueError:
                pin_int = list(sorted(connected_pins[ref])).index(pin) + 1
            elements.append({
                "element_type":    "port",
                "port_id":         _port_id(ref, pin),
                "component_id":    _comp_id(ref),
                "pin_number":      pin_int,
                "name":            str(pin),
                "electrical_type": "passive",  # refined per-net below
            })

    # --- net elements (also refine electrical_type on ports) ---
    # Build port_id -> index map so we can update electrical_type
    port_idx: dict[str, int] = {
        e["port_id"]: i
        for i, e in enumerate(elements)
        if e["element_type"] == "port"
    }

    for net in nets:
        if len(net["nodes"]) < 2:
            warnings.append(
                f"Net '{net['name']}' has only {len(net['nodes'])} node(s); skipping."
            )
            continue

        nclass  = _infer_net_class(net["name"])
        nid     = _net_id(net["name"], net_id_seen)
        port_ids: list[str] = []

        for node in net["nodes"]:
            ref, pin = node["ref"], str(node["pin"])
            pid = _port_id(ref, pin)
            port_ids.append(pid)

            # Refine electrical_type now that we know the net class
            if pid in port_idx:
                meta = comp_meta.get(ref, {})
                pkg_raw = meta.get("footprint", "")
                pkg = _strip_footprint_library(pkg_raw) if pkg_raw else ""
                ctype_str = _infer_component_type(ref, pkg)
                etype = _infer_electrical_type(nclass, ctype_str)
                elements[port_idx[pid]]["electrical_type"] = etype

        elements.append({
            "element_type":      "net",
            "net_id":            nid,
            "name":              net["name"],
            "connected_port_ids": port_ids,
            "net_class":         nclass,
        })

    netlist = {
        "version":      "1.0",
        "project_name": project_name,
        "description":  description or f"Imported from KiCad — {project_name}",
        "elements":     elements,
    }
    return netlist, warnings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def convert_kicad_netlist(
    source_path: str | Path,
    project_name: str | None = None,
    description: str = "",
) -> dict:
    """Convert a KiCad .net or .kicad_sch file to pcb-creator circuit_schema.

    Args:
        source_path:  Path to a KiCad .net (preferred) or .kicad_sch file.
        project_name: Slug for the project (defaults to the file stem,
                      lowercased with spaces → underscores).
        description:  Optional human-readable project description.

    Returns:
        {
            "netlist":  dict,        # circuit_schema.json content
            "warnings": list[str],   # non-fatal issues to surface to user
            "source":   str,         # which file was actually used
        }

    Raises:
        ValueError  if the file type is unsupported or connectivity cannot
                    be determined (e.g. .kicad_sch with no sibling .net).
        FileNotFoundError  if source_path does not exist.
    """
    path = Path(source_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    suffix = path.suffix.lower()

    if suffix not in (".net", ".kicad_sch"):
        raise ValueError(
            f"Unsupported file type '{suffix}'. "
            "Provide a KiCad netlist export (.net) or schematic (.kicad_sch)."
        )

    # Derive project_name from file stem if not provided
    if not project_name:
        project_name = re.sub(r"[^a-z0-9]+", "_", path.stem.lower()).strip("_")
        if not project_name or project_name[0].isdigit():
            project_name = "p_" + project_name

    warnings: list[str] = []
    net_path: Path | None = None
    sch_components: list[dict] = []

    if suffix == ".net":
        net_path = path

    elif suffix == ".kicad_sch":
        # Try sibling .net file (same directory, same stem)
        sibling_net = path.with_suffix(".net")
        if sibling_net.exists():
            net_path = sibling_net
            warnings.append(
                f"Using sibling netlist '{sibling_net.name}' for connectivity. "
                f"Schematic '{path.name}' was used for component metadata."
            )
            # Parse schematic for richer footprint/value data (may override .net)
            sch_components = _parse_kicad_sch_components(path.read_text(encoding="utf-8"))
        else:
            raise ValueError(
                f"Cannot determine net connections from '{path.name}' alone.\n"
                f"In KiCad Schematic Editor: File → Export → Netlist → KiCad format "
                f"→ save as '{path.stem}.net' next to the schematic, then retry."
            )

    # Parse .net for connectivity (always required)
    assert net_path is not None
    net_text = net_path.read_text(encoding="utf-8")
    components, nets = _parse_dot_net(net_text)

    # If we also have schematic components, merge: prefer .kicad_sch footprints
    # since they come from the actual placed symbols (more accurate than .net export)
    if sch_components:
        sch_by_ref = {c["ref"]: c for c in sch_components}
        for comp in components:
            if comp["ref"] in sch_by_ref:
                sch = sch_by_ref[comp["ref"]]
                if sch.get("footprint"):
                    comp["footprint"] = sch["footprint"]
                if sch.get("value"):
                    comp["value"] = sch["value"]

    if not components:
        raise ValueError(
            f"No components found in '{net_path.name}'. "
            "Ensure the file is a valid KiCad netlist export."
        )
    if not nets:
        raise ValueError(
            f"No nets found in '{net_path.name}'. "
            "The schematic may have unconnected components — wire them up in KiCad first."
        )

    netlist, build_warnings = _build_netlist(
        project_name, components, nets, description
    )
    warnings.extend(build_warnings)

    return {
        "netlist":  netlist,
        "warnings": warnings,
        "source":   str(net_path),
    }
