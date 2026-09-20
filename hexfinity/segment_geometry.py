"""Pure-Python (bpy-free) geometry helpers for the Path Segment authoring
tool: convex-hull footprint computation and hull-edge snap targets, built on
top of map.py's generic polygon_edge_snap_points(). Kept separate from
map.py because it's specific to arbitrary user-imported footprints rather
than the hex-grid domain map.py otherwise covers.
"""

import math

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
    for map.edge_snap_points()).

    Despite the "hull" naming (this module's original and still most common
    caller), the underlying math has no convexity requirement — segments.py
    also calls this with a segment's user-authored Corners polygon once it
    becomes the authoritative footprint (see _footprint_local() there),
    which may legitimately be concave."""
    n = max(2, edge_snap)
    per_edge = n - 1
    pts = hexmap.polygon_edge_snap_points(hull_vertices, n)
    return [(x, y, i // per_edge) for i, (x, y) in enumerate(pts)]


def _closest_point_on_edge_xy(px, py, ax, ay, bx, by):
    """Clamped projection of (px, py) onto the finite segment a->b, in
    plain floats (no mathutils.Vector — this module stays bpy-free). Mirrors
    segment_path._closest_point_on_segment's math."""
    abx, aby = bx - ax, by - ay
    denom = abx * abx + aby * aby
    if denom < 1e-12:
        return ax, ay
    t = ((px - ax) * abx + (py - ay) * aby) / denom
    t = max(0.0, min(1.0, t))
    return ax + abx * t, ay + aby * t


def nearest_point_on_hull_edge(x, y, hull_vertices, lock_x=False, lock_y=False):
    """Nearest point on the boundary of the closed polygon `hull_vertices`
    to (x, y), in the X/Y plane, optionally constrained to hold one axis
    fixed -- backs the Add Path Segment Type waypoint list's "Snap to Edge"
    button.

    - lock_x=lock_y=False (default): a true nearest-point-on-boundary
      projection -- every hull edge (closed loop, wrapping i -> (i+1) % n)
      is clamp-projected against via _closest_point_on_edge_xy, and the
      globally closest one wins. Returns (new_x, new_y, edge_idx).
    - exactly one of lock_x/lock_y True: the locked axis's value is held
      fixed, and the other is solved so the result lies exactly on a hull
      edge's *line*, restricted to points within that edge's finite span
      (0 <= t <= 1) -- an edge whose span doesn't cross the locked
      coordinate is skipped, not extrapolated past its endpoints. Among the
      edges that do cross it, the one requiring the smallest movement of
      the free axis wins. Returns None if no edge's span crosses the
      locked coordinate at all (e.g. a locked X outside the hull's X
      range).
    - lock_x and lock_y both True, or len(hull_vertices) < 2: nothing can
      be solved for; returns None.
    """
    n = len(hull_vertices)
    if n < 2 or (lock_x and lock_y):
        return None

    edges = [
        (hull_vertices[i], hull_vertices[(i + 1) % n], i)
        for i in range(n)
    ]

    if not lock_x and not lock_y:
        best = None
        best_dist2 = None
        for (ax, ay), (bx, by), edge_idx in edges:
            cx, cy = _closest_point_on_edge_xy(x, y, ax, ay, bx, by)
            dist2 = (cx - x) ** 2 + (cy - y) ** 2
            if best_dist2 is None or dist2 < best_dist2:
                best_dist2 = dist2
                best = (cx, cy, edge_idx)
        return best

    # Exactly one axis locked: solve the free axis against each edge's
    # infinite line, keep only points landing within that edge's finite
    # span, and pick the one needing the smallest movement of the free axis.
    best = None
    best_move = None
    for (ax, ay), (bx, by), edge_idx in edges:
        if lock_x:
            locked, free_a, free_b, span_a, span_b = x, ax, bx, ay, by
        else:
            locked, free_a, free_b, span_a, span_b = y, ay, by, ax, bx
        d = free_b - free_a
        if abs(d) < 1e-12:
            if abs(free_a - locked) > 1e-9:
                continue
            lo, hi = min(span_a, span_b), max(span_a, span_b)
            free_current = y if lock_x else x
            candidate = max(lo, min(hi, free_current))
        else:
            t = (locked - free_a) / d
            if t < 0.0 or t > 1.0:
                continue
            candidate = span_a + (span_b - span_a) * t

        free_current = y if lock_x else x
        move = abs(candidate - free_current)
        if best_move is None or move < best_move:
            best_move = move
            best = (x, candidate, edge_idx) if lock_x else (candidate, y, edge_idx)

    return best


def sharp_hull_corner_points(hull_vertices, angle_threshold_deg):
    """[(x, y, vertex_index), ...] for every vertex of the closed polygon
    `hull_vertices` whose turn (the angle between its incoming and outgoing
    edge directions) is at least `angle_threshold_deg` — backs the Add
    Corner tool's "snap to a real sharp corner" hint, distinguishing a
    genuine corner from the many near-straight vertices a rounded/filleted
    STL edge's own convex hull otherwise accumulates.

    Mirrors face_select.flood_fill_faces's dot-product/cosine-threshold
    style rather than computing an actual angle via acos/atan2 (neither of
    which is used anywhere else in this codebase): a straight-ahead vertex
    has incoming/outgoing directions that agree (dot == 1, turn == 0), a
    90-degree corner has perpendicular directions (dot == 0), and a full
    reversal has opposing directions (dot == -1, turn == 180). Since a
    *larger* turn angle is a *smaller* dot product, the comparison direction
    is inverted from flood_fill_faces's own ">= cos_threshold" ("close
    enough to the seed"): here a vertex qualifies when
    `dot <= cos_threshold`.

    Returns [] for a degenerate polygon (fewer than 3 vertices). A vertex
    adjacent to a zero-length edge (a duplicate point) is skipped — its
    turn is undefined — the same way nearest_point_on_hull_edge guards a
    near-zero edge length.
    """
    n = len(hull_vertices)
    if n < 3:
        return []

    cos_threshold = math.cos(math.radians(angle_threshold_deg))
    sharp = []
    for i in range(n):
        px, py = hull_vertices[i - 1]
        x, y = hull_vertices[i]
        nx, ny = hull_vertices[(i + 1) % n]

        inx, iny = x - px, y - py
        in_len = math.hypot(inx, iny)
        outx, outy = nx - x, ny - y
        out_len = math.hypot(outx, outy)
        if in_len < 1e-12 or out_len < 1e-12:
            continue
        inx, iny = inx / in_len, iny / in_len
        outx, outy = outx / out_len, outy / out_len

        dot = inx * outx + iny * outy
        if dot <= cos_threshold:
            sharp.append((x, y, i))

    return sharp


def polygon_edge_midpoints(vertices):
    """[(x, y, edge_idx), ...] — the single midpoint of every edge of the
    closed polygon `vertices`, in the same order/winding as `vertices`
    itself (edge i runs vertices[i] -> vertices[(i+1) % n], wrapping n-1
    back to 0), tagged with that edge's index.

    Reuses map.polygon_edge_snap_points(vertices, edge_snap=3) rather than
    reimplementing the (a + b) / 2 arithmetic a second time: at edge_snap=3
    it already emits exactly two points per edge (t=0, the edge's own start
    vertex, and t=0.5, the midpoint), so slicing out every second entry
    starting at index 1 recovers just the midpoints, one per edge."""
    n = len(vertices)
    if n < 2:
        return []
    pts = hexmap.polygon_edge_snap_points(vertices, 3)
    return [(x, y, i) for i, (x, y) in enumerate(pts[1::2])]


def point_in_polygon_concave(x, y, vertices):
    """Standard even-odd (crossing-number) ray-cast point-in-polygon test
    over the closed polygon `vertices`, boundary treatment aside agnostic to
    winding order or convexity — unlike map.point_in_polygon, whose
    docstring notes it assumes a convex polygon for its sign-consistency
    test. Produces the same result as map.point_in_polygon for convex
    input, so it's safe to use unconditionally once a segment's Corners
    polygon (which may be genuinely concave) becomes the authoritative
    footprint (see segments._footprint_local())."""
    n = len(vertices)
    if n < 3:
        return False

    inside = False
    x1, y1 = vertices[-1]
    for (x2, y2) in vertices:
        if (y1 > y) != (y2 > y):
            x_intersect = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_intersect:
                inside = not inside
        x1, y1 = x2, y2
    return inside
