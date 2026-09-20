"""Pure-Python (bpy-free) geometry helpers for the Path Segment authoring
tool: convex-hull footprint computation and hull-edge snap targets, built on
top of map.py's generic polygon_edge_snap_points(). Kept separate from
map.py because it's specific to arbitrary user-imported footprints rather
than the hex-grid domain map.py otherwise covers.
"""

try:
    from . import map as hexmap
except ImportError:
    import map as hexmap


def convex_hull(points):
    """Convex hull of `points` (iterable of (x, y)) via Andrew's monotone
    chain. Returns an ordered, counter-clockwise list of (x, y) hull
    vertices with no repeated closing point and no collinear point kept on
    an edge (only the two endpoints of each hull edge are retained).

    Degenerate input (fewer than 3 distinct points) returns the distinct
    points as-is, in no particular order guarantee beyond stability.
    """
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


def hull_edge_snap_targets(hull_vertices, edge_snap):
    """[(x, y, edge_idx), ...] snap candidates around `hull_vertices` (an
    ordered convex polygon, e.g. convex_hull()'s output), tagged with the
    hull-edge index each point lies on — mirrors how
    path_features._snap_targets_world tags map.edge_snap_points() for hex
    tiles. map.polygon_edge_snap_points() orders its flat result
    edge-by-edge with (edge_snap - 1) points per edge, so the edge index of
    flat position `i` is `i // (edge_snap - 1)` (same convention documented
    for map.edge_snap_points())."""
    n = max(2, edge_snap)
    per_edge = n - 1
    pts = hexmap.polygon_edge_snap_points(hull_vertices, n)
    return [(x, y, i // per_edge) for i, (x, y) in enumerate(pts)]
