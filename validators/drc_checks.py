"""ECAD-grade Design Rule Checks (DRC) for PCB netlists.

Each check function receives parsed netlist data and returns (errors, warnings).
Called by validate_netlist.py after schema and referential integrity pass.
"""

from collections import Counter

from pinout import build_pinout_from_requirements

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
            errors.append(
                f"Net '{name}': {power_out_count} power_out pins — potential short circuit"
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
    components: dict, ports: dict, nets: dict, v_supply: float | None
) -> tuple[list[str], list[str]]:
    errors = []
    warnings = []

    if v_supply is None:
        return errors, warnings

    for cid, comp in components.items():
        if comp.get("component_type") != "resistor":
            continue

        des = comp.get("designator", cid)
        package = comp.get("package", "")

        try:
            r_ohms = parse_resistance(comp.get("value", ""))
        except ValueError:
            continue  # Can't check without a parseable value

        if r_ohms <= 0:
            continue

        rated_power = PACKAGE_POWER.get(package)
        if rated_power is None:
            continue  # Unknown package, skip

        # Determine current through resistor by checking if it's in series with an LED
        res_ports = _component_ports(cid, ports)
        led_vf = None

        for rport in res_ports:
            # Check nets connected to this resistor port — skip power/ground nets
            # since sharing GND doesn't mean the resistor is in series with the LED
            for net in _nets_for_port(rport["port_id"], nets):
                if net.get("net_class") in ("power", "ground"):
                    continue  # Don't match LEDs via shared power/ground rails
                for connected_pid in _ports_on_net(net):
                    conn_comp_id = _port_to_component(connected_pid, ports)
                    if conn_comp_id is None or conn_comp_id == cid:
                        continue
                    conn_comp = components.get(conn_comp_id)
                    if conn_comp and conn_comp.get("component_type") == "led":
                        # Verify this is the LED's anode pin (series connection)
                        led_port = ports.get(connected_pid, {})
                        pin_name = led_port.get("name", "").lower()
                        if pin_name in ("cathode", "k"):
                            continue  # Cathode side = not series, skip
                        # Found LED in series — get its Vf for current calculation
                        props = conn_comp.get("properties", {})
                        try:
                            led_vf = parse_voltage(props.get("vf", ""))
                        except (ValueError, TypeError):
                            # Fall back to defaults based on LED color
                            color = conn_comp.get("value", "").lower()
                            led_vf = LED_VF_DEFAULTS.get(color, 2.0)
                        break
                if led_vf is not None:
                    break
            if led_vf is not None:
                break

        if led_vf is not None:
            # LED series resistor: I = (V_supply - Vf) / R
            i_through = max(0, (v_supply - led_vf) / r_ohms)
            power = (i_through ** 2) * r_ohms
        else:
            # Not in series with an LED — estimate worst case: full supply across resistor
            power = (v_supply ** 2) / r_ohms

        if power > rated_power / RESISTOR_POWER_DERATING:
            if power > rated_power:
                errors.append(
                    f"{des}: power dissipation {power * 1000:.1f}mW exceeds "
                    f"{package} rating {rated_power * 1000:.0f}mW"
                )
            else:
                errors.append(
                    f"{des}: power dissipation {power * 1000:.1f}mW exceeds "
                    f"{package} derated limit {rated_power / RESISTOR_POWER_DERATING * 1000:.0f}mW "
                    f"(2× safety margin)"
                )
        elif power > rated_power / (RESISTOR_POWER_DERATING * 1.33):
            # Within 75% of derated limit — warn
            warnings.append(
                f"{des}: power dissipation {power * 1000:.1f}mW is close to "
                f"{package} derated limit {rated_power / RESISTOR_POWER_DERATING * 1000:.0f}mW"
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
    components: dict, ports: dict, nets: dict, v_supply: float | None
) -> tuple[list[str], list[str]]:
    errors = []
    warnings = []

    if v_supply is None:
        return errors, warnings

    total_current_a = 0.0
    details = []

    for cid, comp in components.items():
        ctype = comp.get("component_type", "")
        des = comp.get("designator", cid)
        props = comp.get("properties", {})

        if ctype == "led":
            if "if" in props:
                try:
                    i = parse_current(props["if"])
                except (ValueError, TypeError):
                    i = LED_IF_DEFAULT
            else:
                i = LED_IF_DEFAULT
            total_current_a += i
            details.append(f"{des}: {i * 1000:.0f}mA")

    if total_current_a > 0:
        total_power_w = v_supply * total_current_a
        warnings.append(
            f"Estimated power budget: {total_current_a * 1000:.0f}mA @ {v_supply}V "
            f"= {total_power_w * 1000:.0f}mW "
            f"({', '.join(details)})"
        )

    return errors, warnings


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
            try:
                v_supply = parse_voltage(voltage_str)
            except ValueError:
                pass

    checks_with_power = [
        check_resistor_power,
        check_capacitor_voltage_rating,
        check_power_budget,
    ]
    for check_fn in checks_with_power:
        errs, warns = check_fn(components, ports, nets, v_supply)
        all_errors.extend(errs)
        all_warnings.extend(warns)

    # Pinout compliance checks (require requirements with pinout data)
    if requirements:
        errs, warns = check_pinout_compliance(components, ports, nets, requirements)
        all_errors.extend(errs)
        all_warnings.extend(warns)

    return all_errors, all_warnings
