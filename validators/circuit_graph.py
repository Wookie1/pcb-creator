"""DC conduction graph and nodal solve for netlist electrical checks.

The older checks in drc_checks.py guessed current from adjacency ("is an LED
next to this resistor?") and otherwise fell back to P = V^2/R, which assumes
the entire supply drops across every resistor. That is wrong for dividers,
pull-ups, shared series resistors, and LEDs in series. This module answers the
question properly: build the DC graph, solve it, read the branch currents.

Scope is deliberately the DC operating point only. Capacitors are open,
inductors are shorts, and ICs / transistors / relays / connectors terminate
analysis (a "boundary") because the suite carries no device models and no
supply-current data for them. A net touching a boundary is reported as
*unevaluated*, never as passing.

ponytail: boundary parts are dropped, never stubbed. Every stub value (open,
0 ohm, a guessed Icc) biases the answer toward "no issue found", and a
confident wrong PASS is worse than no verdict.
"""

from dataclasses import dataclass, field

from engineering_constants import (
    LED_VF_DEFAULTS,
    parse_resistance,
    parse_voltage,
)

# Dynamic resistance of a conducting diode/LED. Real parts are ~10-30 ohm;
# 10 is the conservative (higher-current) end. It also keeps the matrix
# non-singular when a diode sits directly across a rail.
DIODE_DYNAMIC_R = 10.0

# Stand-in for an ideal conductor (inductor, fuse, closed switch). Small
# enough to be electrically irrelevant, non-zero so nodes stay distinct.
WIRE_R = 1e-3

DIODE_VF_DEFAULT = 0.7

_RESISTIVE = {"resistor"}
_SHORTING = {"inductor", "fuse"}
_DIODE = {"led", "diode"}
_NON_CONDUCTING_DC = {"capacitor", "crystal"}
# Everything else terminates analysis. Connectors are included because a
# 2-pin header's pins are not connected to *each other* - they are where the
# outside world attaches.
_BOUNDARY = {
    "ic",
    "voltage_regulator",
    "transistor_npn",
    "transistor_pnp",
    "transistor_nmos",
    "transistor_pmos",
    "relay",
    "connector",
}

MAX_DIODE_ITERATIONS = 5


@dataclass
class Branch:
    """A two-terminal conducting element between two nets.

    Current is positive flowing net_a -> net_b. `vdrop` is a forward voltage
    offset in that same direction (diodes only, 0 for passives).
    """

    comp_id: str
    designator: str
    component_type: str
    net_a: str
    net_b: str
    resistance: float
    vdrop: float = 0.0
    conducting: bool = True
    current: float = 0.0
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def power(self) -> float:
        """Power dissipated in the resistive part of this branch (watts)."""
        return self.current * self.current * self.resistance


@dataclass
class Solution:
    node_v: dict[str, float]
    branches: list[Branch]
    rails: dict[str, float]
    boundary_nets: set[str]
    tainted_nets: set[str]
    floating_nets: list[str]
    unmodeled: list[dict]
    shorted: list[dict]
    assumptions: list[str]
    solved: bool

    def branch_for(self, comp_id: str) -> Branch | None:
        for b in self.branches:
            if b.comp_id == comp_id:
                return b
        return None

    def is_tainted(self, net_id: str) -> bool:
        """True if this net's voltage depends on unmodeled black-box behavior."""
        return net_id in self.tainted_nets

    def branch_is_tainted(self, branch: Branch) -> bool:
        return self.is_tainted(branch.net_a) or self.is_tainted(branch.net_b)


def propagate_taint(
    branches: list[Branch], boundary_nets: set[str], rails: dict[str, float]
) -> set[str]:
    """Nets whose voltage depends on a part we have no model for.

    Seeded from nets touching a boundary pin, then spread along branches -
    an LED one resistor downstream of an MCU pin is just as unknowable as the
    pin itself, and reads a misleading 0mA if treated as trustworthy.

    A declared rail blocks the spread: an ideal rail holds its voltage no
    matter what an IC hanging off it does, so it is trusted even though nearly
    every IC on the board touches it.
    """
    tainted = {n for n in boundary_nets if n not in rails}
    changed = True
    while changed:
        changed = False
        for b in branches:
            for src, dst in ((b.net_a, b.net_b), (b.net_b, b.net_a)):
                if src in tainted and dst not in tainted and dst not in rails:
                    tainted.add(dst)
                    changed = True
    return tainted

def _port_net_map(nets: dict) -> dict[str, str]:
    """Map port_id -> net_id. A port on several nets takes the first."""
    mapping: dict[str, str] = {}
    for nid, net in nets.items():
        for pid in net.get("connected_port_ids", []):
            mapping.setdefault(pid, nid)
    return mapping


def _diode_vf(comp: dict) -> tuple[float, str]:
    """Forward voltage for a diode/LED, with the provenance of the number."""
    props = comp.get("properties") or {}
    if "vf" in props:
        try:
            return parse_voltage(str(props["vf"])), "netlist"
        except (ValueError, TypeError):
            pass
    if comp.get("component_type") == "led":
        color = str(comp.get("value", "")).strip().lower()
        if color in LED_VF_DEFAULTS:
            return LED_VF_DEFAULTS[color], "default_table"
        return LED_VF_DEFAULTS["red"], "assumed"
    return DIODE_VF_DEFAULT, "default_table"


def _anode_first(comp_ports: list[dict]) -> list[dict]:
    """Order a diode's two ports anode-first.

    Ports carry anode/cathode names by default (see circuit_builder), so the
    name checks below decide it; pin-number order is only a last resort for
    unnamed explicit pinouts.
    """
    for p in comp_ports:
        name = str(p.get("name", "")).strip().lower()
        if name in ("anode", "a", "+", "p"):
            return [p, [q for q in comp_ports if q is not p][0]]
        if name in ("cathode", "k", "c", "-", "n"):
            return [[q for q in comp_ports if q is not p][0], p]
    return sorted(comp_ports, key=lambda p: p.get("pin_number", 0))


def build_branches(
    components: dict, ports: dict, nets: dict
) -> tuple[list[Branch], set[str], list[dict], list[dict], list[str]]:
    """Turn the netlist into conducting branches plus boundary information.

    Returns (branches, boundary_nets, unmodeled, shorted, assumptions).
    """
    port_net = _port_net_map(nets)
    by_comp: dict[str, list[dict]] = {}
    for port in ports.values():
        by_comp.setdefault(port.get("component_id"), []).append(port)

    branches: list[Branch] = []
    boundary_nets: set[str] = set()
    unmodeled: list[dict] = []
    shorted: list[dict] = []
    assumptions: list[str] = []

    for cid, comp in components.items():
        ctype = comp.get("component_type", "")
        des = comp.get("designator", cid)
        comp_ports = by_comp.get(cid, [])
        attached = [p for p in comp_ports if port_net.get(p["port_id"])]

        if ctype in _BOUNDARY:
            nets_touched = {port_net[p["port_id"]] for p in attached}
            boundary_nets |= nets_touched
            unmodeled.append(
                {
                    "designator": des,
                    "value": comp.get("value", ""),
                    "component_type": ctype,
                    "nets": sorted(nets_touched),
                }
            )
            continue

        if ctype in _NON_CONDUCTING_DC:
            continue  # open at DC

        if ctype == "switch":
            if len(attached) != 2:
                boundary_nets |= {port_net[p["port_id"]] for p in attached}
                unmodeled.append(
                    {
                        "designator": des,
                        "value": comp.get("value", ""),
                        "component_type": ctype,
                        "nets": sorted(port_net[p["port_id"]] for p in attached),
                    }
                )
                continue
            assumptions.append(f"{des}: switch assumed closed (worst case for current)")

        if len(attached) > 2:
            # More terminals than we can model - typically a potentiometer
            # typed as a resistor, whose wiper position a netlist never
            # states. Treat as a boundary so downstream results are marked
            # indeterminate rather than silently reading 0mA.
            nets_touched = {port_net[p["port_id"]] for p in attached}
            boundary_nets |= nets_touched
            unmodeled.append(
                {
                    "designator": des,
                    "value": comp.get("value", ""),
                    "component_type": ctype,
                    "nets": sorted(nets_touched),
                    "reason": f"{len(attached)} terminals; only 2-terminal parts are modeled",
                }
            )
            continue

        if len(attached) < 2:
            continue  # a dangling pin carries no current - correctly modeled

        net_a = port_net[attached[0]["port_id"]]
        net_b = port_net[attached[1]["port_id"]]
        if net_a == net_b:
            # Both pins on one net. Carries no current and does nothing - an
            # easy mistake for an agent calling connect_pins.
            shorted.append(
                {"designator": des, "component_type": ctype, "net": net_a}
            )
            continue

        if ctype in _RESISTIVE:
            try:
                r = parse_resistance(comp.get("value", ""))
            except ValueError:
                unmodeled.append(
                    {
                        "designator": des,
                        "value": comp.get("value", ""),
                        "component_type": ctype,
                        "nets": sorted({net_a, net_b}),
                        "reason": "unparseable resistance",
                    }
                )
                boundary_nets |= {net_a, net_b}
                continue
            if r <= 0:
                r = WIRE_R
            branches.append(
                Branch(cid, des, ctype, net_a, net_b, r, sources={"r": "netlist"})
            )

        elif ctype in _SHORTING or ctype == "switch":
            branches.append(
                Branch(cid, des, ctype, net_a, net_b, WIRE_R, sources={"r": "assumed"})
            )

        elif ctype in _DIODE:
            ordered = _anode_first(attached)
            vf, vf_src = _diode_vf(comp)
            branches.append(
                Branch(
                    cid,
                    des,
                    ctype,
                    port_net[ordered[0]["port_id"]],
                    port_net[ordered[1]["port_id"]],
                    DIODE_DYNAMIC_R,
                    vdrop=vf,
                    sources={"vf": vf_src, "rd": "assumed"},
                )
            )

    return branches, boundary_nets, unmodeled, shorted, assumptions


def _reachable(seeds: set[str], branches: list[Branch], conducting_only: bool) -> set[str]:
    """Nets reachable from `seeds` by walking branches."""
    adj: dict[str, set[str]] = {}
    for b in branches:
        if conducting_only and not b.conducting:
            continue
        adj.setdefault(b.net_a, set()).add(b.net_b)
        adj.setdefault(b.net_b, set()).add(b.net_a)

    seen = set(seeds)
    stack = list(seeds)
    while stack:
        n = stack.pop()
        for m in adj.get(n, ()):
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return seen


def _solve_nodal(
    branches: list[Branch], known: dict[str, float], unknown: list[str]
) -> dict[str, float] | None:
    """Solve KCL for `unknown` nodes given fixed voltages at `known` nodes.

    Every source in this domain is an ideal ground-referenced DC rail, so the
    known nodes fold into the right-hand side and no MNA augmentation is needed.
    """
    if not unknown:
        return {}

    import numpy as np

    idx = {n: i for i, n in enumerate(unknown)}
    size = len(unknown)
    g = np.zeros((size, size))
    rhs = np.zeros(size)

    for b in branches:
        if not b.conducting:
            continue
        conductance = 1.0 / b.resistance
        # Current leaving net_a through this branch:
        #   (v_a - v_b - vdrop) / R
        for node, other, sign in ((b.net_a, b.net_b, 1.0), (b.net_b, b.net_a, -1.0)):
            if node not in idx:
                continue
            i = idx[node]
            g[i][i] += conductance
            if other in idx:
                g[i][idx[other]] -= conductance
            else:
                rhs[i] += known[other] * conductance
            rhs[i] += sign * b.vdrop * conductance

    try:
        volts = np.linalg.solve(g, rhs)
    except np.linalg.LinAlgError:
        return None
    return {n: float(volts[idx[n]]) for n in unknown}


def solve(
    components: dict,
    ports: dict,
    nets: dict,
    rails: dict[str, float],
) -> Solution:
    """Solve the DC operating point.

    `rails` maps net_id -> forced voltage and must already include ground at
    0V. Callers decide rail assignment (see drc_checks._resolve_rails) so this
    module never guesses a supply from a net name.
    """
    branches, boundary_nets, unmodeled, shorted, assumptions = build_branches(
        components, ports, nets
    )

    # Structural floating: unreachable from any rail using *all* branches,
    # ignoring conduction state. A net reachable only through a reverse-biased
    # diode is not a design error, so it must not land here.
    structurally_connected = _reachable(set(rails), branches, conducting_only=False)
    floating = [
        nid
        for nid in nets
        if nid not in structurally_connected
        and nid not in boundary_nets
        and any(nid in (b.net_a, b.net_b) for b in branches)
    ]

    node_v: dict[str, float] = dict(rails)
    solved = True

    for _ in range(MAX_DIODE_ITERATIONS):
        live = _reachable(set(rails), branches, conducting_only=True)
        unknown = sorted(n for n in live if n not in rails)
        result = _solve_nodal(branches, rails, unknown)
        if result is None:
            solved = False
            break

        node_v = dict(rails)
        node_v.update(result)

        for b in branches:
            if b.net_a in node_v and b.net_b in node_v:
                b.current = (
                    node_v[b.net_a] - node_v[b.net_b] - b.vdrop
                ) / b.resistance
            else:
                b.current = 0.0

        # Piecewise-linear diode update: a conducting diode carrying reverse
        # current opens; a blocked diode with enough forward bias closes.
        changed = False
        for b in branches:
            if b.component_type not in _DIODE:
                continue
            if b.conducting and b.current < 0:
                b.conducting = False
                b.current = 0.0
                changed = True
            elif not b.conducting:
                va = node_v.get(b.net_a)
                vb = node_v.get(b.net_b)
                if va is not None and vb is not None and (va - vb) > b.vdrop:
                    b.conducting = True
                    changed = True
        if not changed:
            break

    for b in branches:
        if not b.conducting:
            b.current = 0.0

    return Solution(
        node_v=node_v,
        branches=branches,
        rails=rails,
        boundary_nets=boundary_nets,
        tainted_nets=propagate_taint(branches, boundary_nets, rails),
        floating_nets=sorted(floating),
        unmodeled=unmodeled,
        shorted=shorted,
        assumptions=assumptions,
        solved=solved,
    )
