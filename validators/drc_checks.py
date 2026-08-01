"""ECAD-grade Design Rule Checks (DRC) for PCB netlists.

Each check function receives parsed netlist data and returns (errors, warnings).
Called by validate_netlist.py after schema and referential integrity pass.
"""

from collections import Counter

from pinout import build_pinout_from_requirements

import circuit_graph
from engineering_constants import (
    CAPACITOR_MAX_F,
    CAPACITOR_MIN_F,
    CERAMIC_VOLTAGE_DERATING,
    DECOUPLING_CAP_F,
    DECOUPLING_CAP_TOLERANCE,
    ELECTROLYTIC_VOLTAGE_DERATING,
    LED_IF_DEFAULT,
    LED_VF_DEFAULTS,
    PACKAGE_POWER,
    RESISTOR_MAX_OHM,
    RESISTOR_MIN_OHM,
    RESISTOR_POWER_DERATING,
    parse_capacitance,
    parse_current,
    parse_resistance,
    parse_supply_voltage,
    parse_voltage,
)


# ---------------------------------------------------------------------------
# Helper: build lookup structures from flat element lists
# ---------------------------------------------------------------------------

def build_lookups(elements: list[dict]) -> tuple[dict, dict, dict]:
    """Parse elements into components, ports, nets dicts keyed by ID."""
    components = {}
    ports = {}
    nets = {}
    for elem in elements:
        etype = elem.get("element_type")
        if etype == "component":
            components[elem["component_id"]] = elem
        elif etype == "port":
            ports[elem["port_id"]] = elem
        elif etype == "net":
            nets[elem["net_id"]] = elem
    return components, ports, nets


def _port_to_component(port_id: str, ports: dict) -> str | None:
    """Get the component_id that owns a port."""
    port = ports.get(port_id)
    return port["component_id"] if port else None


def _component_designator(comp_id: str, components: dict) -> str:
    """Get human-readable designator for a component."""
    comp = components.get(comp_id)
    return comp["designator"] if comp else comp_id


def _ports_on_net(net: dict) -> list[str]:
    """Get port IDs connected to a net."""
    return net.get("connected_port_ids", [])


def _nets_for_port(port_id: str, nets: dict) -> list[dict]:
    """Find all nets a port belongs to."""
    return [n for n in nets.values() if port_id in n.get("connected_port_ids", [])]


def _component_ports(comp_id: str, ports: dict) -> list[dict]:
    """Get all ports belonging to a component."""
    return [p for p in ports.values() if p.get("component_id") == comp_id]


# ---------------------------------------------------------------------------
# Helper: rail assignment and the DC solve
# ---------------------------------------------------------------------------

# Appended to any finding whose inputs depend on a part we have no model for.
# Such a finding is reported as a warning, never an error: the number is only
# valid while the unmodeled pin is high-Z, and we cannot know that.
_INDETERMINATE = " [indeterminate: depends on an unmodeled part]"

def _regulator_output_nets(components: dict, ports: dict, nets: dict) -> set[str]:
    """Nets driven by a regulator output, identified by pin name only.

    Pin *position* is not usable: a 3-pin regulator's pinout differs between
    SOT-23 and TO-220, so guessing by number would silently invert Vin/Vout.
    """
    driven: set[str] = set()
    for cid, comp in components.items():
        if comp.get("component_type") != "voltage_regulator":
            continue
        for port in _component_ports(cid, ports):
            name = str(port.get("name", "")).strip().lower()
            if "out" in name or name == "vo":
                for net in _nets_for_port(port["port_id"], nets):
                    driven.add(net["net_id"])
    return driven


def resolve_rails(
    components: dict, ports: dict, nets: dict, v_supply: float | None
) -> tuple[dict[str, float], list[str]]:
    """Assign fixed node voltages: ground to 0V, the single input rail to v_supply.

    Returns (rails, ambiguous). When more than one power net could be the input
    rail, we assign none of them and report them as ambiguous - the caller asks
    for an explicit mapping rather than guessing. Net names do not encode the
    voltage reliably (the most common power net name in this corpus is a bare
    "VCC", which says nothing).
    """
    rails = {
        nid: 0.0 for nid, net in nets.items() if net.get("net_class") == "ground"
    }
    if v_supply is None:
        return rails, []

    driven = _regulator_output_nets(components, ports, nets)
    candidates = sorted(
        nid
        for nid, net in nets.items()
        if net.get("net_class") == "power" and nid not in driven
    )
    if len(candidates) == 1:
        rails[candidates[0]] = v_supply
        return rails, []
    return rails, candidates


def solve_circuit(
    components: dict,
    ports: dict,
    nets: dict,
    v_supply: float | None,
    rail_overrides: dict[str, float] | None = None,
) -> circuit_graph.Solution | None:
    """Build and solve the DC operating point, or None if no rail is known."""
    rails, _ambiguous = resolve_rails(components, ports, nets, v_supply)
    if rail_overrides:
        rails.update(rail_overrides)
    if not any(v != 0.0 for v in rails.values()):
        return None  # ground only - nothing to drive current
    return circuit_graph.solve(components, ports, nets, rails)


# ---------------------------------------------------------------------------
# 1. Single-pin nets
# ---------------------------------------------------------------------------

def check_single_pin_nets(
    components: dict, ports: dict, nets: dict
) -> tuple[list[str], list[str]]:
    errors = []
    warnings = []

    for nid, net in nets.items():
        port_ids = _ports_on_net(net)
        name = net.get("name", nid)

        # Duplicate port_ids in the same net
        counts = Counter(port_ids)
        for pid, cnt in counts.items():
            if cnt > 1:
                errors.append(
                    f"Net '{name}': port '{pid}' listed {cnt} times (duplicate)"
                )

        # All ports belong to same component
        unique_ports = set(port_ids)
        comp_ids = {_port_to_component(pid, ports) for pid in unique_ports}
        comp_ids.discard(None)
        if len(comp_ids) == 1 and len(unique_ports) >= 2:
            des = _component_designator(comp_ids.pop(), components)
            warnings.append(
                f"Net '{name}': all ports belong to component {des} — likely a mistake"
            )

    return errors, warnings


# ---------------------------------------------------------------------------
# 2. Duplicate nets
# ---------------------------------------------------------------------------

def check_duplicate_nets(
    components: dict, ports: dict, nets: dict
) -> tuple[list[str], list[str]]:
    errors = []
    warnings = []

    seen: dict[frozenset[str], str] = {}
    for nid, net in nets.items():
        key = frozenset(_ports_on_net(net))
        name = net.get("name", nid)
        if key in seen:
            errors.append(
                f"Nets '{seen[key]}' and '{name}' connect identical ports — redundant"
            )
        else:
            seen[key] = name

    return errors, warnings


# ---------------------------------------------------------------------------
# 3. Net class vs pin type consistency
# ---------------------------------------------------------------------------

def check_net_class_vs_pin_types(
    components: dict, ports: dict, nets: dict
) -> tuple[list[str], list[str]]:
    errors = []
    warnings = []

    for nid, net in nets.items():
        name = net.get("name", nid)
        net_class = net.get("net_class", "")
        port_ids = _ports_on_net(net)
        etypes = {ports[pid].get("electrical_type") for pid in port_ids if pid in ports}

        if net_class == "ground":
            if "power_out" in etypes:
                errors.append(
                    f"Net '{name}' (ground): has power_out pin — likely wiring error"
                )
            if "ground" not in etypes:
                warnings.append(
                    f"Net '{name}' (ground): no pin has electrical_type 'ground'"
                )

        elif net_class == "power":
            if etypes and etypes <= {"signal"}:
                warnings.append(
                    f"Net '{name}' (power): all pins are signal type — should this be a signal net?"
                )

    return errors, warnings


# ---------------------------------------------------------------------------
# 4. Pin type conflicts (short circuits)
# ---------------------------------------------------------------------------

def check_pin_type_conflicts(
    components: dict, ports: dict, nets: dict
) -> tuple[list[str], list[str]]:
    errors = []
    warnings = []

    for nid, net in nets.items():
        name = net.get("name", nid)
        port_ids = _ports_on_net(net)
        etypes = [ports[pid].get("electrical_type") for pid in port_ids if pid in ports]

        power_out_count = etypes.count("power_out")
        if power_out_count >= 2:
            # Multiple power sources on the same rail is common (e.g., USB VBUS +
            # voltage regulator output both feed VCC_5V via protection diodes).
            warnings.append(
                f"Net '{name}': {power_out_count} power_out pins — verify sources are isolated (diode/switch)"
            )

    return errors, warnings


# ---------------------------------------------------------------------------
# 5. Component value sanity
# ---------------------------------------------------------------------------

def check_component_value_sanity(
    components: dict, ports: dict, nets: dict
) -> tuple[list[str], list[str]]:
    errors = []
    warnings = []

    for cid, comp in components.items():
        ctype = comp.get("component_type", "")
        value = comp.get("value", "")
        des = comp.get("designator", cid)

        if ctype == "resistor":
            try:
                ohms = parse_resistance(value)
                if ohms < RESISTOR_MIN_OHM:
                    warnings.append(
                        f"{des}: resistance {value} is extremely low (<{RESISTOR_MIN_OHM}Ω)"
                    )
                elif ohms > RESISTOR_MAX_OHM:
                    warnings.append(
                        f"{des}: resistance {value} is extremely high (>{RESISTOR_MAX_OHM / 1e6:g}MΩ)"
                    )
            except ValueError:
                warnings.append(f"{des}: cannot parse resistance value '{value}'")

        elif ctype == "capacitor":
            try:
                farads = parse_capacitance(value)
                if farads < CAPACITOR_MIN_F:
                    warnings.append(
                        f"{des}: capacitance {value} is extremely small (<1pF)"
                    )
                elif farads > CAPACITOR_MAX_F:
                    warnings.append(
                        f"{des}: capacitance {value} is extremely large (>10mF)"
                    )
            except ValueError:
                warnings.append(f"{des}: cannot parse capacitance value '{value}'")

    return errors, warnings


# ---------------------------------------------------------------------------
# 6. Missing decoupling capacitors for ICs
# ---------------------------------------------------------------------------

def check_decoupling_capacitors(
    components: dict, ports: dict, nets: dict
) -> tuple[list[str], list[str]]:
    errors = []
    warnings = []

    ic_types = {"ic", "voltage_regulator"}
    vcc_pin_names = {"vcc", "vdd", "v+", "vin", "vout"}

    for cid, comp in components.items():
        if comp.get("component_type") not in ic_types:
            continue

        des = comp.get("designator", cid)
        ic_ports = _component_ports(cid, ports)

        # Find power input pins
        power_pins = [
            p for p in ic_ports
            if p.get("electrical_type") in ("power_in", "power_out")
            and p.get("name", "").lower() in vcc_pin_names
        ]

        for ppin in power_pins:
            pid = ppin["port_id"]
            pin_nets = _nets_for_port(pid, nets)
            if not pin_nets:
                continue

            # Check if any capacitor is on the same net
            has_decoupling = False
            for net in pin_nets:
                for connected_pid in _ports_on_net(net):
                    if connected_pid == pid:
                        continue
                    connected_comp_id = _port_to_component(connected_pid, ports)
                    if connected_comp_id is None:
                        continue
                    connected_comp = components.get(connected_comp_id)
                    if connected_comp and connected_comp.get("component_type") == "capacitor":
                        try:
                            cap_value = parse_capacitance(connected_comp.get("value", ""))
                            lo = DECOUPLING_CAP_F * (1 - DECOUPLING_CAP_TOLERANCE)
                            hi = DECOUPLING_CAP_F * (1 + DECOUPLING_CAP_TOLERANCE)
                            if lo <= cap_value <= hi:
                                has_decoupling = True
                                break
                        except ValueError:
                            pass
                if has_decoupling:
                    break

            if not has_decoupling:
                net_name = pin_nets[0].get("name", "unknown") if pin_nets else "unknown"
                warnings.append(
                    f"{des}: VCC pin '{ppin['name']}' on net '{net_name}' "
                    f"has no 100nF decoupling capacitor"
                )

    return errors, warnings


# ---------------------------------------------------------------------------
# 7. Resistor power rating check
# ---------------------------------------------------------------------------

def check_resistor_power(
    components: dict,
    ports: dict,
    nets: dict,
    v_supply: float | None,
    solution: "circuit_graph.Solution | None" = None,
) -> tuple[list[str], list[str]]:
    """Flag resistors dissipating more than their package can shed.

    Power comes from the solved branch current (P = I^2 R), so this is correct
    for dividers, pull-ups, shared series resistors, and LEDs in series - all
    cases the previous adjacency heuristic got wrong.
    """
    errors = []
    warnings = []

    if v_supply is None:
        return errors, warnings

    sol = solution or solve_circuit(components, ports, nets, v_supply)
    if sol is None:
        return errors, warnings

    for branch in sol.branches:
        if branch.component_type != "resistor":
            continue

        comp = components.get(branch.comp_id, {})
        des = branch.designator
        package = comp.get("package", "")

        rated_power = PACKAGE_POWER.get(package)
        if rated_power is None:
            continue  # Unknown package, skip

        power = branch.power
        derated = rated_power / RESISTOR_POWER_DERATING

        if power > derated:
            # Suggest a fix: larger package, or enough resistance to stay under
            # the derated limit at this branch's operating voltage.
            fix_suggestions = []
            for alt_pkg, alt_rating in sorted(PACKAGE_POWER.items(), key=lambda x: x[1]):
                if alt_rating >= power * RESISTOR_POWER_DERATING and alt_pkg != package:
                    fix_suggestions.append(alt_pkg)
                    break
            if fix_suggestions:
                fix_hint = f" Change {des} to package {fix_suggestions[0]}."
            else:
                v_across = abs(branch.current) * branch.resistance
                alt_r = int((v_across ** 2) / derated) if derated > 0 else 0
                fix_hint = (
                    f" Increase {des} resistance above {alt_r}Ω, or use a larger package."
                )

            if power > rated_power:
                msg = (
                    f"{des}: power dissipation {power * 1000:.1f}mW exceeds "
                    f"{package} rating {rated_power * 1000:.0f}mW.{fix_hint}"
                )
            else:
                msg = (
                    f"{des}: power dissipation {power * 1000:.1f}mW exceeds "
                    f"{package} derated limit {derated * 1000:.0f}mW "
                    f"(2× safety margin).{fix_hint}"
                )
            if sol.branch_is_tainted(branch):
                warnings.append(msg + _INDETERMINATE)
            else:
                errors.append(msg)
        elif power > rated_power / (RESISTOR_POWER_DERATING * 1.33):
            # Within 75% of derated limit — warn
            warnings.append(
                f"{des}: power dissipation {power * 1000:.1f}mW is close to "
                f"{package} derated limit {derated * 1000:.0f}mW"
            )

    return errors, warnings


# ---------------------------------------------------------------------------
# 8. Capacitor voltage rating check
# ---------------------------------------------------------------------------

def check_capacitor_voltage_rating(
    components: dict, ports: dict, nets: dict, v_supply: float | None
) -> tuple[list[str], list[str]]:
    errors = []
    warnings = []

    if v_supply is None:
        return errors, warnings

    for cid, comp in components.items():
        if comp.get("component_type") != "capacitor":
            continue

        props = comp.get("properties", {})
        voltage_rating_str = props.get("voltage_rating")
        if not voltage_rating_str:
            continue

        des = comp.get("designator", cid)

        try:
            v_rated = parse_voltage(voltage_rating_str)
        except ValueError:
            warnings.append(f"{des}: cannot parse voltage_rating '{voltage_rating_str}'")
            continue

        # Determine if electrolytic or ceramic
        cap_type = props.get("type", "").lower()
        value_str = comp.get("value", "").lower()
        is_electrolytic = "electrolytic" in cap_type or "electrolytic" in value_str

        derating = ELECTROLYTIC_VOLTAGE_DERATING if is_electrolytic else CERAMIC_VOLTAGE_DERATING
        required_v = v_supply * derating

        if v_rated < required_v:
            errors.append(
                f"{des}: voltage rating {voltage_rating_str} is below "
                f"{'electrolytic' if is_electrolytic else 'ceramic'} derating "
                f"requirement ({derating}× {v_supply}V = {required_v}V)"
            )

    return errors, warnings


# ---------------------------------------------------------------------------
# 9. Power budget estimation
# ---------------------------------------------------------------------------

def check_power_budget(
    components: dict,
    ports: dict,
    nets: dict,
    v_supply: float | None,
    solution: "circuit_graph.Solution | None" = None,
) -> tuple[list[str], list[str]]:
    """Report the current actually drawn from the supply rail.

    Summed from solved branch currents leaving the rail, so it counts only
    loads that are really on that rail and really have a path to ground -
    unlike the previous LED_IF_DEFAULT x count estimate, which counted every
    LED on the board whether powered or not.
    """
    errors = []
    warnings = []

    if v_supply is None:
        return errors, warnings

    sol = solution or solve_circuit(components, ports, nets, v_supply)
    if sol is None:
        return errors, warnings

    supply_nets = {nid for nid, v in sol.rails.items() if v != 0.0}
    total_current_a = 0.0
    details = []
    tainted = False

    for branch in sol.branches:
        # Current leaving a supply rail into the rest of the circuit.
        if branch.net_a in supply_nets:
            i = branch.current
        elif branch.net_b in supply_nets:
            i = -branch.current
        else:
            continue
        if i <= 0:
            continue
        total_current_a += i
        details.append(f"{branch.designator}: {i * 1000:.1f}mA")
        if sol.branch_is_tainted(branch):
            tainted = True

    if total_current_a > 0:
        total_power_w = v_supply * total_current_a
        note = _INDETERMINATE if tainted else ""
        warnings.append(
            f"Power budget: {total_current_a * 1000:.0f}mA @ {v_supply}V "
            f"= {total_power_w * 1000:.0f}mW "
            f"({', '.join(details)}){note}"
        )

    return errors, warnings


# ---------------------------------------------------------------------------
# Solve-derived checks
# ---------------------------------------------------------------------------

# The schema's port.electrical_type enum, verbatim from circuit_schema.json.
# Off-enum values are reported rather than asserted on: netlists in projects/
# carry "bidirectional" on 50 ports, which the schema does not allow. That
# value is emitted by scripts/test_stm32_4layer.py, not by the LLM pipeline or
# circuit_builder, so it is a fixture bug to fix at the source rather than a
# gap to widen the schema for.
VALID_ELECTRICAL_TYPES = frozenset(
    {"power_in", "power_out", "signal", "ground", "passive", "no_connect"}
)


def _lookup_curated(value: str) -> dict:
    """Curated part specs, or {} when the orchestrator package is unavailable."""
    try:
        from orchestrator.gather.curated_specs import lookup_specs
    except ImportError:  # pragma: no cover - validators can run standalone
        return {}
    try:
        return lookup_specs(value) or {}
    except Exception:  # pragma: no cover - lookup is best-effort
        return {}


def check_led_current(
    components: dict,
    ports: dict,
    nets: dict,
    v_supply: float | None,
    solution: "circuit_graph.Solution | None" = None,
) -> tuple[list[str], list[str]]:
    """Flag LEDs driven past their forward-current rating, or too dim to see."""
    errors: list[str] = []
    warnings: list[str] = []

    if v_supply is None:
        return errors, warnings
    sol = solution or solve_circuit(components, ports, nets, v_supply)
    if sol is None:
        return errors, warnings

    for branch in sol.branches:
        if branch.component_type != "led" or not branch.conducting:
            continue
        comp = components.get(branch.comp_id, {})
        props = comp.get("properties") or {}
        i_max = LED_IF_DEFAULT
        try:
            i_max = parse_current(str(props["if"]))
        except (KeyError, ValueError, TypeError):
            pass

        i = branch.current
        if i > i_max:
            r_series = sum(
                b.resistance
                for b in sol.branches
                if b.component_type == "resistor"
                and abs(abs(b.current) - abs(i)) < 1e-9
            )
            suggested = int((v_supply - branch.vdrop) / i_max) if i_max > 0 else 0
            msg = (
                f"{branch.designator}: forward current {i * 1000:.1f}mA exceeds "
                f"{i_max * 1000:.0f}mA rating. Increase the series resistance to "
                f"at least {suggested}Ω."
            )
            if sol.branch_is_tainted(branch) or r_series == 0:
                warnings.append(msg + _INDETERMINATE)
            else:
                errors.append(msg)
        elif 0 < i < i_max * 0.05 and not sol.branch_is_tainted(branch):
            # An LED driven by an MCU pin always reads ~0mA here because the
            # driver is unmodeled - that is our blind spot, not a dim LED.
            warnings.append(
                f"{branch.designator}: forward current {i * 1000:.2f}mA is under 5% "
                f"of its {i_max * 1000:.0f}mA rating — likely too dim to see."
            )

    return errors, warnings


def check_rail_voltage(
    components: dict,
    ports: dict,
    nets: dict,
    v_supply: float | None,
    solution: "circuit_graph.Solution | None" = None,
) -> tuple[list[str], list[str]]:
    """Flag parts whose supply pin sits outside their datasheet voltage range.

    This is the highest-value check available at the black-box boundary: we
    cannot model what an IC does, but we can check that we are not destroying it.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if v_supply is None:
        return errors, warnings
    sol = solution or solve_circuit(components, ports, nets, v_supply)
    if sol is None:
        return errors, warnings

    for cid, comp in components.items():
        if comp.get("component_type") not in ("ic", "voltage_regulator"):
            continue
        props = comp.get("properties") or {}
        specs = {**_lookup_curated(str(comp.get("value", ""))), **props}

        bounds = {}
        for key in ("vcc_min", "vcc_max"):
            if key in specs:
                try:
                    bounds[key] = parse_voltage(str(specs[key]))
                except (ValueError, TypeError):
                    pass
        if not bounds:
            continue

        des = comp.get("designator", cid)
        for port in _component_ports(cid, ports):
            if port.get("electrical_type") != "power_in":
                continue
            for net in _nets_for_port(port["port_id"], nets):
                v = sol.node_v.get(net["net_id"])
                if v is None:
                    continue
                if sol.is_tainted(net["net_id"]):
                    # Typically a regulator output: nothing in our model drives
                    # this net, so it solves to an artifact voltage. Belongs in
                    # not_checked, not in a finding.
                    continue
                lo, hi = bounds.get("vcc_min"), bounds.get("vcc_max")
                if hi is not None and v > hi:
                    errors.append(
                        f"{des}: supply pin {port.get('name', port['pin_number'])} on net "
                        f"{net.get('name')} is at {v:.2f}V, above its {hi:g}V maximum. "
                        f"This will damage the part."
                    )
                elif lo is not None and v < lo:
                    warnings.append(
                        f"{des}: supply pin {port.get('name', port['pin_number'])} on net "
                        f"{net.get('name')} is at {v:.2f}V, below its {lo:g}V minimum — "
                        f"the part may not start."
                    )

    return errors, warnings


def check_circuit_integrity(
    components: dict,
    ports: dict,
    nets: dict,
    v_supply: float | None = None,
    solution: "circuit_graph.Solution | None" = None,
) -> tuple[list[str], list[str]]:
    """Structural faults that need no supply voltage: shorts, floats, bad refs."""
    errors: list[str] = []
    warnings: list[str] = []

    # A port listed on more than one net is a genuine short between them.
    port_nets: dict[str, list[str]] = {}
    for nid, net in nets.items():
        for pid in net.get("connected_port_ids", []):
            port_nets.setdefault(pid, []).append(nid)

    for pid, nids in sorted(port_nets.items()):
        if len(nids) > 1:
            des = _component_designator(_port_to_component(pid, ports) or "", components)
            names = ", ".join(sorted(nets[n].get("name", n) for n in nids))
            errors.append(
                f"{des}: pin {ports.get(pid, {}).get('name', pid)} is on {len(nids)} "
                f"nets ({names}) — these nets are shorted together."
            )
        if pid not in ports:
            errors.append(f"Net references unknown port {pid}.")

    for port in sorted(ports.values(), key=lambda p: p.get("port_id", "")):
        etype = port.get("electrical_type")
        if etype not in VALID_ELECTRICAL_TYPES:
            des = _component_designator(port.get("component_id", ""), components)
            warnings.append(
                f"{des}: pin {port.get('name', port.get('pin_number'))} has "
                f"electrical_type '{etype}', which is not a schema-valid value."
            )

    sol = solution
    if sol is None and v_supply is not None:
        sol = solve_circuit(components, ports, nets, v_supply)
    if sol is None:
        return errors, warnings

    for entry in sol.shorted:
        errors.append(
            f"{entry['designator']}: both pins are on net "
            f"{nets.get(entry['net'], {}).get('name', entry['net'])} — the part is "
            f"shorted out and carries no current."
        )

    for nid in sol.floating_nets:
        errors.append(
            f"Net {nets.get(nid, {}).get('name', nid)} has no DC path to any supply "
            f"or ground — this section of the circuit is isolated."
        )

    return errors, warnings


def check_pullups(
    components: dict, ports: dict, nets: dict
) -> tuple[list[str], list[str]]:
    """Warn when an I2C-style net has no resistor to a supply rail.

    Warning only: many MCUs can enable internal pull-ups in firmware, which is
    not visible in a netlist.
    """
    warnings: list[str] = []
    power_nets = {
        nid for nid, net in nets.items() if net.get("net_class") == "power"
    }

    for nid, net in sorted(nets.items()):
        name = str(net.get("name", "")).strip().upper()
        if not any(tag in name for tag in ("SDA", "SCL", "I2C")):
            continue

        has_pullup = False
        for pid in net.get("connected_port_ids", []):
            cid = _port_to_component(pid, ports)
            comp = components.get(cid or "", {})
            if comp.get("component_type") != "resistor":
                continue
            for other in _component_ports(cid, ports):
                if other["port_id"] == pid:
                    continue
                if any(
                    n["net_id"] in power_nets
                    for n in _nets_for_port(other["port_id"], nets)
                ):
                    has_pullup = True
        if not has_pullup:
            warnings.append(
                f"Net {net.get('name', nid)} looks like an I2C bus but has no pull-up "
                f"resistor to a supply rail. Add one (typically 4.7kohm) unless the "
                f"driver enables an internal pull-up."
            )

    return [], warnings


# ---------------------------------------------------------------------------
# Check: IC pinout compliance
# ---------------------------------------------------------------------------

def check_pinout_compliance(
    components: dict, ports: dict, nets: dict,
    requirements: dict | None = None,
) -> tuple[list[str], list[str]]:
    """Verify that netlist ports match the IC pinout from requirements.

    This runs AFTER auto-correction in validate_netlist, so errors here
    represent unfixable issues (e.g. pin_number out of range).
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not requirements:
        return errors, warnings

    pinouts = build_pinout_from_requirements(requirements)
    if not pinouts:
        return errors, warnings

    # Build designator -> component_id lookup
    des_to_comp: dict[str, str] = {}
    for cid, comp in components.items():
        des_to_comp[comp.get("designator", "")] = cid

    # Build component_id -> designator for port lookups
    comp_to_des: dict[str, str] = {}
    for cid, comp in components.items():
        comp_to_des[cid] = comp.get("designator", "")

    # Group ports by parent component
    comp_ports: dict[str, list[dict]] = {}
    for port in ports.values():
        cid = port.get("component_id", "")
        comp_ports.setdefault(cid, []).append(port)

    for designator, pin_map in pinouts.items():
        comp_id = des_to_comp.get(designator)
        if comp_id is None:
            continue  # Component not in netlist (may be unused)

        port_list = comp_ports.get(comp_id, [])
        used_pins: set[int] = set()

        for port in port_list:
            pin_num = port.get("pin_number")
            port_id = port.get("port_id", "?")

            if pin_num not in pin_map:
                errors.append(
                    f"DRC pinout: {designator} {port_id} has pin_number {pin_num} "
                    f"which is not in the {len(pin_map)}-pin pinout"
                )
                continue

            used_pins.add(pin_num)
            expected = pin_map[pin_num]

            # Check name match (case-insensitive, any of primary/alt)
            port_name = port.get("name", "").upper().strip()
            expected_upper = [n.upper() for n in expected.all_names]
            if port_name and port_name not in expected_upper:
                # Also check if the full "A/B" name matches
                full_name_upper = "/".join(expected.all_names).upper()
                if port_name != full_name_upper:
                    errors.append(
                        f"DRC pinout: {designator} {port_id} pin {pin_num} "
                        f"name '{port.get('name', '')}' doesn't match expected "
                        f"'{'/'.join(expected.all_names)}'"
                    )

            # Check electrical type
            current_type = port.get("electrical_type", "")
            if current_type != expected.inferred_electrical_type:
                warnings.append(
                    f"DRC pinout: {designator} {port_id} pin {pin_num} "
                    f"type '{current_type}' differs from expected "
                    f"'{expected.inferred_electrical_type}'"
                )

        # Check for missing pins (warning only — NC pins may be intentionally absent)
        missing = set(pin_map.keys()) - used_pins
        if missing:
            missing_sorted = sorted(missing)
            # Only warn for non-NC pins
            nc_missing = [p for p in missing_sorted
                          if pin_map[p].inferred_electrical_type == "no_connect"]
            real_missing = [p for p in missing_sorted
                           if pin_map[p].inferred_electrical_type != "no_connect"]
            if real_missing:
                pins_str = ", ".join(
                    f"{p}:{pin_map[p].primary_name}" for p in real_missing
                )
                warnings.append(
                    f"DRC pinout: {designator} missing ports for pins: {pins_str}"
                )

    return errors, warnings


# ---------------------------------------------------------------------------
# Public API: run all DRC checks
# ---------------------------------------------------------------------------

def run_all_drc_checks(
    elements: list[dict],
    requirements: dict | None = None,
) -> tuple[list[str], list[str]]:
    """Run all DRC checks on a parsed netlist.

    Args:
        elements: The 'elements' array from the netlist JSON.
        requirements: Optional requirements dict (for power-aware checks).

    Returns:
        (errors, warnings) tuple.
    """
    components, ports, nets = build_lookups(elements)

    all_errors: list[str] = []
    all_warnings: list[str] = []

    # Net topology checks (no requirements needed)
    checks_no_reqs = [
        check_single_pin_nets,
        check_duplicate_nets,
        check_net_class_vs_pin_types,
        check_pin_type_conflicts,
        check_component_value_sanity,
        check_decoupling_capacitors,
        check_pullups,
    ]
    for check_fn in checks_no_reqs:
        errs, warns = check_fn(components, ports, nets)
        all_errors.extend(errs)
        all_warnings.extend(warns)

    # Power-aware checks (require V_supply from requirements)
    v_supply = None
    if requirements:
        power = requirements.get("power", {})
        voltage_str = power.get("voltage")
        if voltage_str:
            v_supply = parse_supply_voltage(voltage_str)

    # Solve once and share the result across every solve-derived check.
    solution = solve_circuit(components, ports, nets, v_supply)

    checks_with_power = [
        check_resistor_power,
        check_capacitor_voltage_rating,
        check_power_budget,
        check_led_current,
        check_rail_voltage,
        check_circuit_integrity,
    ]
    for check_fn in checks_with_power:
        if check_fn is check_capacitor_voltage_rating:
            errs, warns = check_fn(components, ports, nets, v_supply)
        else:
            errs, warns = check_fn(components, ports, nets, v_supply, solution)
        all_errors.extend(errs)
        all_warnings.extend(warns)

    # Pinout compliance checks (require requirements with pinout data)
    if requirements:
        errs, warns = check_pinout_compliance(components, ports, nets, requirements)
        all_errors.extend(errs)
        all_warnings.extend(warns)

    return all_errors, all_warnings
