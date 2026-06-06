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

# Net classes delivered by copper pours/planes rather than point-to-point
# traces — excluded from the SA crossing metric (see IncrementalCost).
_PLANE_NET_CLASSES = frozenset({"power", "ground"})


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
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4

    # Bounding-box reject: if the segments' AABBs don't overlap they can't cross.
    # Cheap test that eliminates the vast majority of pairs before the products.
    if min(x1, x2) > max(x3, x4) or max(x1, x2) < min(x3, x4):
        return False
    if min(y1, y2) > max(y3, y4) or max(y1, y2) < min(y3, y4):
        return False

    # Inlined orientation cross-products (avoids per-call closure overhead):
    # d1,d2 = orientation of p1,p2 about segment p3p4; d3,d4 vice versa.
    dx34, dy34 = x4 - x3, y4 - y3
    d1 = dx34 * (y1 - y3) - dy34 * (x1 - x3)
    d2 = dx34 * (y2 - y3) - dy34 * (x2 - x3)
    dx12, dy12 = x2 - x1, y2 - y1
    d3 = dx12 * (y3 - y1) - dy12 * (x3 - x1)
    d4 = dx12 * (y4 - y1) - dy12 * (x4 - x1)

    return (((d1 > 0) != (d2 > 0)) and (d1 != 0) and (d2 != 0)
            and ((d3 > 0) != (d4 > 0)) and (d3 != 0) and (d4 != 0))


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


class IncrementalCost:
    """Delta-evaluation engine for SA placement cost (wire length + crossings).

    A simulated-annealing move perturbs only a few components, yet the naive
    cost function recomputes every net's MST and an O(E^2) all-pairs crossing
    scan on every iteration.  This class caches per-net MST geometry and the
    flat segment list so a move only recomputes the nets touching the moved
    components and only re-tests crossings involving those segments —
    O(|affected| * E) instead of O(E^2).

    Usage per SA iteration:
        wire, cross = ev.evaluate(new_positions, changed_designators)
        if accept: ev.commit()
        else:      ev.revert()

    `evaluate` mutates internal state to the candidate; `revert` restores the
    pre-move state exactly, `commit` keeps it.  Totals stay drift-free because
    unaffected segments are never touched and affected nets are fully recomputed.
    """

    def __init__(
        self,
        nets: list[NetInfo],
        positions: dict[str, tuple[float, float]],
    ) -> None:
        self._net_designators: list[list[str]] = []  # filtered to present positions
        self._net_seg_start: list[int] = []
        self._net_seg_count: list[int] = []
        self._net_wire: list[float] = []
        # Power/ground nets are delivered by copper pours, not point-to-point
        # traces, so their MST "crossings" are meaningless.  Plane nets keep a
        # (cheap) wirelength term for loose clustering but are excluded from the
        # crossing metric and never enter the spatial grid — which also removes
        # the board-spanning hub segments that otherwise dominate the cost.
        self._net_is_plane: list[bool] = []
        self._segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
        self._seg_net: list[int] = []
        self._des_to_nets: dict[str, set[int]] = {}

        for i, net in enumerate(nets):
            self._net_is_plane.append(net.net_class in _PLANE_NET_CLASSES)
            ds = [d for d in net.designators if d in positions]
            self._net_designators.append(ds)
            start = len(self._segments)
            self._net_seg_start.append(start)
            if len(ds) < 2:
                self._net_seg_count.append(0)
                self._net_wire.append(0.0)
                continue
            pts = [positions[d] for d in ds]
            mst = compute_mst_edges(pts)
            wl = 0.0
            for ia, ib, length in mst:
                self._segments.append((pts[ia], pts[ib]))
                self._seg_net.append(i)
                wl += length
            self._net_seg_count.append(len(mst))
            self._net_wire.append(wl)
            for d in ds:
                self._des_to_nets.setdefault(d, set()).add(i)

        # Uniform spatial grid over segments so a crossing query only tests
        # segments sharing a cell, not all E.  Cell size ~ mean segment span
        # keeps a typical segment in ~1 cell.
        spans = [max(abs(p1[0] - p2[0]), abs(p1[1] - p2[1]))
                 for p1, p2 in self._segments]
        self._cell: float = max(1.0, (sum(spans) / len(spans)) if spans else 1.0)
        self._grid: dict[tuple[int, int], set[int]] = {}
        self._seg_cells: list[list[tuple[int, int]]] = [[] for _ in self._segments]
        for i in range(len(self._segments)):
            self._grid_insert(i)

        self.total_wire: float = sum(self._net_wire)
        self.total_cross: int = self._crossings_involving(range(len(self._segments)))

        # Pending-move backup (set by evaluate, consumed by commit/revert)
        self._bk_active = False
        self._bk_segs: dict[int, tuple[tuple[float, float], tuple[float, float]]] = {}
        self._bk_wire: dict[int, float] = {}
        self._bk_total_wire = 0.0
        self._bk_total_cross = 0

    def _cells_for(
        self, seg: tuple[tuple[float, float], tuple[float, float]]
    ) -> list[tuple[int, int]]:
        """Grid cells covered by a segment's bounding box."""
        (x1, y1), (x2, y2) = seg
        cs = self._cell
        cx0 = int(math.floor(min(x1, x2) / cs))
        cx1 = int(math.floor(max(x1, x2) / cs))
        cy0 = int(math.floor(min(y1, y2) / cs))
        cy1 = int(math.floor(max(y1, y2) / cs))
        return [(cx, cy) for cx in range(cx0, cx1 + 1)
                for cy in range(cy0, cy1 + 1)]

    def _grid_insert(self, i: int) -> None:
        if self._net_is_plane[self._seg_net[i]]:
            return  # plane-net segments never participate in crossing tests
        cells = self._cells_for(self._segments[i])
        self._seg_cells[i] = cells
        g = self._grid
        for c in cells:
            bucket = g.get(c)
            if bucket is None:
                g[c] = {i}
            else:
                bucket.add(i)

    def _grid_remove(self, i: int) -> None:
        g = self._grid
        for c in self._seg_cells[i]:
            bucket = g.get(c)
            if bucket is not None:
                bucket.discard(i)
                if not bucket:
                    del g[c]
        self._seg_cells[i] = []

    def _crossings_involving(self, seg_indices) -> int:
        """Count crossings where at least one segment is in seg_indices.

        Each qualifying pair is counted once: for an affected-affected pair the
        skip rule `b in S and b <= a` ensures it's tallied only when `a` is the
        smaller index.  Passing the full index range yields the exact total.
        """
        segments = self._segments
        seg_net = self._seg_net
        is_plane = self._net_is_plane
        grid = self._grid
        seg_cells = self._seg_cells
        S = set(seg_indices)
        cnt = 0
        for a in S:
            na = seg_net[a]
            if is_plane[na]:
                continue  # plane segments contribute no crossings
            (ax1, ay1), (ax2, ay2) = segments[a]
            cand: set[int] = set()
            for c in seg_cells[a]:
                cand.update(grid.get(c, ()))
            for b in cand:
                if b == a:
                    continue
                if b <= a and b in S:
                    continue
                if seg_net[b] == na:
                    continue
                sb = segments[b]
                if _segments_intersect((ax1, ay1), (ax2, ay2), sb[0], sb[1]):
                    cnt += 1
        return cnt

    def _recompute_net(
        self, net_idx: int, positions: dict[str, tuple[float, float]]
    ) -> float:
        """Rewrite a net's segment slice in place at new positions; return wire length."""
        count = self._net_seg_count[net_idx]
        if count == 0:
            return 0.0
        ds = self._net_designators[net_idx]
        start = self._net_seg_start[net_idx]
        pts = [positions[d] for d in ds]
        mst = compute_mst_edges(pts)
        wl = 0.0
        for k, (ia, ib, length) in enumerate(mst):
            self._segments[start + k] = (pts[ia], pts[ib])
            wl += length
        return wl

    def evaluate(
        self,
        positions: dict[str, tuple[float, float]],
        changed_designators,
    ) -> tuple[float, int]:
        """Apply a candidate move and return (total_wire, total_cross).

        Mutates internal state to the candidate. Caller must then call commit()
        (keep) or revert() (undo) before the next evaluate().
        """
        affected_nets: set[int] = set()
        for d in changed_designators:
            affected_nets.update(self._des_to_nets.get(d, ()))

        affected_segs: list[int] = []
        for ni in affected_nets:
            s = self._net_seg_start[ni]
            affected_segs.extend(range(s, s + self._net_seg_count[ni]))

        # Backup for potential revert
        self._bk_segs = {i: self._segments[i] for i in affected_segs}
        self._bk_wire = {ni: self._net_wire[ni] for ni in affected_nets}
        self._bk_total_wire = self.total_wire
        self._bk_total_cross = self.total_cross
        self._bk_active = True

        if not affected_nets:
            return self.total_wire, self.total_cross

        old_cross_local = self._crossings_involving(affected_segs)
        for i in affected_segs:
            self._grid_remove(i)
        for ni in affected_nets:
            new_wl = self._recompute_net(ni, positions)
            self.total_wire += new_wl - self._net_wire[ni]
            self._net_wire[ni] = new_wl
        for i in affected_segs:
            self._grid_insert(i)
        new_cross_local = self._crossings_involving(affected_segs)
        self.total_cross += new_cross_local - old_cross_local
        return self.total_wire, self.total_cross

    def commit(self) -> None:
        """Keep the last evaluated move."""
        self._bk_active = False
        self._bk_segs = {}
        self._bk_wire = {}

    def revert(self) -> None:
        """Undo the last evaluated move, restoring exact pre-move state."""
        if not self._bk_active:
            return
        affected = list(self._bk_segs.keys())
        for i in affected:
            self._grid_remove(i)               # drop new-geometry cells
        for i, seg in self._bk_segs.items():
            self._segments[i] = seg            # restore old geometry
        for i in affected:
            self._grid_insert(i)               # re-add at old-geometry cells
        for ni, wl in self._bk_wire.items():
            self._net_wire[ni] = wl
        self.total_wire = self._bk_total_wire
        self.total_cross = self._bk_total_cross
        self._bk_active = False
        self._bk_segs = {}
        self._bk_wire = {}
