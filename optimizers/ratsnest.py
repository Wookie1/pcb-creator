"""Ratsnest computation — connectivity graph, MST, and cost metrics for placement optimization."""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class NetInfo:
    """A net's connectivity expressed as component designators."""
    net_id: str
    name: str
    net_class: str  # "signal", "power", "ground"
    designators: list[str]  # component designators connected by this net


@dataclass
class RatsnestResult:
    """Cost metrics for a placement."""
    total_wire_length: float
    crossing_count: int
    mst_edges: list[tuple[str, str, float]] = field(default_factory=list)  # (des_a, des_b, length)


@dataclass
class DecouplingAssociation:
    """A decoupling capacitor associated with an IC."""
    cap_designator: str
    ic_designator: str
    power_net: str     # shared power net name
    ground_net: str    # shared ground net name


@dataclass
class CrystalAssociation:
    """A crystal/oscillator associated with an MCU/IC."""
    crystal_designator: str
    ic_designator: str
    connected_nets: list[str] = field(default_factory=list)  # net names connecting them


_IC_TYPES = {"ic", "microcontroller", "mcu", "voltage_regulator"}
_CAP_TYPES = {"capacitor"}
_CRYSTAL_TYPES = {"crystal", "oscillator"}


def _build_element_lookups(netlist: dict) -> tuple[
    dict[str, dict],          # components: component_id -> element
    dict[str, list[dict]],    # ports_by_comp: component_id -> [port elements]
    dict[str, dict],          # nets_by_id: net_id -> net element
    dict[str, str],           # port_to_net: port_id -> net_id
]:
    """Parse netlist elements into lookup tables."""
    components: dict[str, dict] = {}
    ports_by_comp: dict[str, list[dict]] = {}
    nets_by_id: dict[str, dict] = {}
    port_to_net: dict[str, str] = {}

    for elem in netlist.get("elements", []):
        etype = elem.get("element_type")
        if etype == "component":
            components[elem["component_id"]] = elem
        elif etype == "port":
            ports_by_comp.setdefault(elem["component_id"], []).append(elem)
        elif etype == "net":
            nets_by_id[elem["net_id"]] = elem
            for pid in elem.get("connected_port_ids", []):
                port_to_net[pid] = elem["net_id"]

    return components, ports_by_comp, nets_by_id, port_to_net


def _get_nets_by_class(
    comp_id: str,
    ports_by_comp: dict[str, list[dict]],
    port_to_net: dict[str, str],
    nets_by_id: dict[str, dict],
) -> tuple[set[str], set[str], set[str]]:
    """Get power, ground, and signal net_ids for a component."""
    power_nets: set[str] = set()
    ground_nets: set[str] = set()
    signal_nets: set[str] = set()
    for port in ports_by_comp.get(comp_id, []):
        net_id = port_to_net.get(port["port_id"])
        if net_id:
            net = nets_by_id.get(net_id)
            if net:
                nc = net.get("net_class", "signal")
                if nc == "power":
                    power_nets.add(net_id)
                elif nc == "ground":
                    ground_nets.add(net_id)
                else:
                    signal_nets.add(net_id)
    return power_nets, ground_nets, signal_nets


def find_decoupling_associations(netlist: dict) -> list[DecouplingAssociation]:
    """Identify capacitor-IC pairs that form decoupling relationships.

    A capacitor is a decoupling cap for an IC if they share both a power net
    AND a ground net.
    """
    components, ports_by_comp, nets_by_id, port_to_net = _build_element_lookups(netlist)

    caps = [c for c in components.values()
            if c.get("component_type", "").lower() in _CAP_TYPES]
    ics = [c for c in components.values()
           if c.get("component_type", "").lower() in _IC_TYPES]

    results = []
    for cap in caps:
        cap_power, cap_gnd, _ = _get_nets_by_class(
            cap["component_id"], ports_by_comp, port_to_net, nets_by_id)
        for ic in ics:
            ic_power, ic_gnd, _ = _get_nets_by_class(
                ic["component_id"], ports_by_comp, port_to_net, nets_by_id)
            shared_power = cap_power & ic_power
            shared_gnd = cap_gnd & ic_gnd
            if shared_power and shared_gnd:
                pnet = nets_by_id[next(iter(shared_power))].get("name", "")
                gnet = nets_by_id[next(iter(shared_gnd))].get("name", "")
                results.append(DecouplingAssociation(
                    cap_designator=cap["designator"],
                    ic_designator=ic["designator"],
                    power_net=pnet,
                    ground_net=gnet,
                ))

    return sorted(results, key=lambda a: (a.ic_designator, a.cap_designator))


def find_crystal_associations(netlist: dict) -> list[CrystalAssociation]:
    """Identify crystal-IC pairs connected via shared nets."""
    components, ports_by_comp, nets_by_id, port_to_net = _build_element_lookups(netlist)

    crystals = [c for c in components.values()
                if c.get("component_type", "").lower() in _CRYSTAL_TYPES]
    ics = [c for c in components.values()
           if c.get("component_type", "").lower() in _IC_TYPES]

    results = []
    for xtal in crystals:
        xtal_all = set()
        for port in ports_by_comp.get(xtal["component_id"], []):
            nid = port_to_net.get(port["port_id"])
            if nid:
                xtal_all.add(nid)

        for ic in ics:
            ic_all = set()
            for port in ports_by_comp.get(ic["component_id"], []):
                nid = port_to_net.get(port["port_id"])
                if nid:
                    ic_all.add(nid)

            shared = xtal_all & ic_all
            if shared:
                net_names = [nets_by_id[nid].get("name", nid) for nid in shared]
                results.append(CrystalAssociation(
                    crystal_designator=xtal["designator"],
                    ic_designator=ic["designator"],
                    connected_nets=sorted(net_names),
                ))

    return sorted(results, key=lambda a: (a.ic_designator, a.crystal_designator))


def build_connectivity(netlist: dict) -> list[NetInfo]:
    """Build connectivity graph from netlist elements.

    Returns a list of NetInfo, each describing which component designators
    a net connects.  Single-pin nets and nets connecting only one component
    are excluded (they contribute nothing to routing cost).
    """
    elements = netlist.get("elements", [])

    # Build lookup tables
    components: dict[str, dict] = {}  # component_id -> element
    ports: dict[str, dict] = {}       # port_id -> element

    for elem in elements:
        etype = elem.get("element_type")
        if etype == "component":
            components[elem["component_id"]] = elem
        elif etype == "port":
            ports[elem["port_id"]] = elem

    # Map port_id -> designator
    port_to_designator: dict[str, str] = {}
    for pid, port in ports.items():
        comp = components.get(port.get("component_id", ""))
        if comp:
            port_to_designator[pid] = comp["designator"]

    # Build NetInfo list
    nets: list[NetInfo] = []
    for elem in elements:
        if elem.get("element_type") != "net":
            continue

        connected_ports = elem.get("connected_port_ids", [])
        designators: list[str] = []
        seen: set[str] = set()
        for pid in connected_ports:
            des = port_to_designator.get(pid)
            if des and des not in seen:
                designators.append(des)
                seen.add(des)

        # Only include nets connecting 2+ distinct components
        if len(designators) >= 2:
            nets.append(NetInfo(
                net_id=elem["net_id"],
                name=elem.get("name", elem["net_id"]),
                net_class=elem.get("net_class", "signal"),
                designators=designators,
            ))

    return nets


def _manhattan(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Manhattan distance between two points."""
    return abs(p1[0] - p2[0]) + abs(p1[1] - p2[1])


def compute_mst_edges(
    positions: list[tuple[float, float]],
) -> list[tuple[int, int, float]]:
    """Compute minimum spanning tree edges using Prim's algorithm with Manhattan distance.

    Args:
        positions: List of (x, y) coordinates for each node.

    Returns:
        List of (index_a, index_b, distance) edges forming the MST.
    """
    n = len(positions)
    if n <= 1:
        return []
    if n == 2:
        return [(0, 1, _manhattan(positions[0], positions[1]))]

    # Prim's algorithm
    in_mst = [False] * n
    min_cost = [math.inf] * n
    min_edge = [-1] * n  # which MST node connects to this node
    edges: list[tuple[int, int, float]] = []

    # Start from node 0
    min_cost[0] = 0.0
    for _ in range(n):
        # Find the cheapest non-MST node
        u = -1
        for v in range(n):
            if not in_mst[v] and (u == -1 or min_cost[v] < min_cost[u]):
                u = v
        in_mst[u] = True
        if min_edge[u] != -1:
            edges.append((min_edge[u], u, min_cost[u]))

        # Update costs for neighbors
        for v in range(n):
            if not in_mst[v]:
                d = _manhattan(positions[u], positions[v])
                if d < min_cost[v]:
                    min_cost[v] = d
                    min_edge[v] = u

    return edges


def total_wire_length(
    nets: list[NetInfo],
    positions: dict[str, tuple[float, float]],
) -> float:
    """Compute total MST wire length across all nets.

    Args:
        nets: List of NetInfo from build_connectivity().
        positions: Map of designator -> (x_mm, y_mm).

    Returns:
        Total estimated wire length in mm.
    """
    total = 0.0
    for net in nets:
        pts = [positions[d] for d in net.designators if d in positions]
        if len(pts) < 2:
            continue
        for _, _, length in compute_mst_edges(pts):
            total += length
    return total


def _segments_intersect(
    p1: tuple[float, float], p2: tuple[float, float],
    p3: tuple[float, float], p4: tuple[float, float],
) -> bool:
    """Check if line segments (p1-p2) and (p3-p4) intersect (proper crossing only).

    Uses the cross-product orientation test. Excludes collinear overlap and
    endpoint touching — we only want true crossings.
    """
    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    d1 = cross(p3, p4, p1)
    d2 = cross(p3, p4, p2)
    d3 = cross(p1, p2, p3)
    d4 = cross(p1, p2, p4)

    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True

    return False


def count_crossings(
    nets: list[NetInfo],
    positions: dict[str, tuple[float, float]],
) -> int:
    """Count the number of MST edge crossings across all nets.

    Each pair of crossing edges from different nets counts as one crossing.
    """
    # Collect all MST edges as line segments
    all_segments: list[tuple[tuple[float, float], tuple[float, float], str]] = []

    for net in nets:
        pts = [positions[d] for d in net.designators if d in positions]
        if len(pts) < 2:
            continue
        mst = compute_mst_edges(pts)
        for ia, ib, _ in mst:
            all_segments.append((pts[ia], pts[ib], net.net_id))

    crossings = 0
    for i in range(len(all_segments)):
        for j in range(i + 1, len(all_segments)):
            # Only count crossings between different nets
            if all_segments[i][2] == all_segments[j][2]:
                continue
            if _segments_intersect(
                all_segments[i][0], all_segments[i][1],
                all_segments[j][0], all_segments[j][1],
            ):
                crossings += 1

    return crossings


def compute_cost(
    nets: list[NetInfo],
    positions: dict[str, tuple[float, float]],
    wire_weight: float = 1.0,
    crossing_weight: float = 5.0,
) -> RatsnestResult:
    """Compute weighted placement cost.

    Args:
        nets: Connectivity info.
        positions: Designator -> (x, y) map.
        wire_weight: Weight for total wire length.
        crossing_weight: Weight for crossing count.

    Returns:
        RatsnestResult with metrics and combined cost accessible via
        total_wire_length and crossing_count.
    """
    wl = total_wire_length(nets, positions)
    cx = count_crossings(nets, positions)

    # Build flat list of MST edges for visualization
    mst_edges: list[tuple[str, str, float]] = []
    for net in nets:
        pts = [positions[d] for d in net.designators if d in positions]
        des_list = [d for d in net.designators if d in positions]
        if len(pts) < 2:
            continue
        for ia, ib, length in compute_mst_edges(pts):
            mst_edges.append((des_list[ia], des_list[ib], length))

    return RatsnestResult(
        total_wire_length=wl,
        crossing_count=cx,
        mst_edges=mst_edges,
    )
