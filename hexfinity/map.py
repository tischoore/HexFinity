"""Map-level math for HexFinity: tile positions, neighbour lookup, and
shared-corner correspondences for flat-top hex grids with odd-q offset.

Pure-Python — no bpy import. `find_tile` reads attributes from a Blender
scene/object that the caller hands in, but does not import bpy itself, so
the module stays unit-testable in plain CPython.
"""

import math


N = "N"
NE = "NE"
SE = "SE"
S = "S"
SW = "SW"
NW = "NW"

DIRECTIONS = (N, NE, SE, S, SW, NW)

OPPOSITE = {N: S, S: N, NE: SW, SW: NE, SE: NW, NW: SE}


# Corner indices 0..5 map to P1..P6 in the flat-top layout:
#   0 = P1 upper-right
#   1 = P2 right
#   2 = P3 lower-right
#   3 = P4 lower-left
#   4 = P5 left
#   5 = P6 upper-left
#
# Each entry lists the (up to two) neighbours that geometrically meet this
# corner, paired with the corner index on the neighbour that coincides.
# The table is independent of q-parity: it encodes the fixed geometric
# relationship between corners on adjacent hexes. Parity only affects which
# (q', r') is the neighbour — that lives in neighbour_coord.
SHARED_CORNERS = (
    ((N,  2), (NE, 4)),   # P1 upper-right ↔ N.P3  + NE.P5
    ((NE, 3), (SE, 5)),   # P2 right       ↔ NE.P4 + SE.P6
    ((SE, 4), (S,  0)),   # P3 lower-right ↔ SE.P5 + S.P1
    ((S,  5), (SW, 1)),   # P4 lower-left  ↔ S.P6  + SW.P2
    ((SW, 0), (NW, 2)),   # P5 left        ↔ SW.P1 + NW.P3
    ((NW, 1), (N,  3)),   # P6 upper-left  ↔ NW.P2 + N.P4
)


# Edge i runs from corner_xy(i) to corner_xy((i+1)%6). Exactly one neighbour
# direction is common to SHARED_CORNERS[i] and SHARED_CORNERS[(i+1)%6] --
# that's the single tile bordering this edge (a corner touches two
# neighbours, but an edge, being shared by only two tiles total, touches
# just one). Derived by hand from SHARED_CORNERS; consumed by
# path_features.py to resolve which tile a Path Feature line crosses into
# when a click snaps onto one of this tile's own edge_snap_points().
# test_edge_directions_consistent_with_shared_corners in test_map.py
# verifies this against SHARED_CORNERS directly so the two tables can't
# silently drift apart.
EDGE_DIRECTIONS = (NE, SE, S, SW, NW, N)


def clamp_level(v, min_level=0):
    """Floor a corner level at `min_level`. Mirrors the IntProperty(min=0)
    constraint on the per-tile pN properties; kept bpy-free so the multi-select
    delta logic stays unit-testable."""
    return v if v > min_level else min_level


def apply_corner_delta(values, corner_idx, delta, min_level=0):
    """Return a new 6-tuple equal to `values` with `delta` added to the entry
    at `corner_idx`, clamped at `min_level`. Used by the multi-select parallel
    corner edit to compute each selected tile's new corner level."""
    out = list(values)
    out[corner_idx] = clamp_level(out[corner_idx] + delta, min_level)
    return tuple(out)


def neighbour_coord(q, r, direction):
    """Return (q', r') of the neighbour of tile (q, r) in `direction`.
    Odd-q offset (flat-top): odd columns are shifted +Y by half a row."""
    odd = (q & 1) == 1
    if direction == N:
        return (q, r + 1)
    if direction == S:
        return (q, r - 1)
    if direction == NE:
        return (q + 1, r + 1) if odd else (q + 1, r)
    if direction == SE:
        return (q + 1, r) if odd else (q + 1, r - 1)
    if direction == NW:
        return (q - 1, r + 1) if odd else (q - 1, r)
    if direction == SW:
        return (q - 1, r) if odd else (q - 1, r - 1)
    raise ValueError(f"unknown direction: {direction!r}")


def missing_neighbours(existing):
    """(q, r) coords adjacent to at least one coord in `existing` but not
    themselves in it. One entry per open grid slot, deduplicated across
    however many existing tiles border it (a slot bordered by three
    existing tiles still yields exactly one coordinate here).

    `existing`: any iterable of (q, r) tuples. Pure data in, pure data out.
    """
    existing_set = set(existing)
    missing = set()
    for (q, r) in existing_set:
        for direction in DIRECTIONS:
            n = neighbour_coord(q, r, direction)
            if n not in existing_set:
                missing.add(n)
    return missing


def resolve_new_tile_corners(q, r, lookup, base_level=0):
    """(p1..p6) levels to seed a brand-new tile at (q, r) with, so it welds
    onto whichever existing neighbours already border it.

    `lookup`: {(q, r): (p1..p6)} for existing tiles. This is the read
    direction of the exact same SHARED_CORNERS walk operators.on_corner_changed
    already does in the write direction when propagating an edit outward —
    here we pull inward into a tile that doesn't exist yet. A corner with no
    existing neighbour falls back to `base_level` (mirrors how a freshly
    generated tile's corners are seeded). If both SHARED_CORNERS partners for
    a corner exist, they are expected to already agree — that invariant is
    exactly what on_corner_changed maintains while editing an existing map —
    so the first table entry is used if they ever disagree.
    """
    corners = []
    for corner_idx in range(6):
        value = None
        for (direction, n_corner_idx) in SHARED_CORNERS[corner_idx]:
            n_values = lookup.get(neighbour_coord(q, r, direction))
            if n_values is not None:
                value = n_values[n_corner_idx]
                break
        corners.append(base_level if value is None else value)
    return tuple(corners)


def tile_world_xy(q, r, diameter_mm):
    """World-space (x, y) of tile (q, r)'s origin for a flat-top, odd-q
    offset layout with the given point-to-point diameter (mm)."""
    R = diameter_mm / 2.0
    col_pitch = 1.5 * R
    row_pitch = R * math.sqrt(3.0)
    x = q * col_pitch
    y = r * row_pitch + (row_pitch / 2.0 if (q & 1) else 0.0)
    return (x, y)


def corner_xy(i, diameter_mm):
    """Tile-local (x, y) of P-corner `i` (0=P1..5=P6), flat-top convention.

    Matches the angle formula used by mesh_builder.build_hex_tile's
    corners_xy and overlay._tile_corner_world_positions — kept here as the
    single canonical definition for new bpy-free callers (those two sites
    predate this helper and are left as-is, see path_features design)."""
    R = diameter_mm / 2.0
    angle = math.pi / 3.0 - i * (math.pi / 3.0)
    return (R * math.cos(angle), R * math.sin(angle))


def polygon_edge_snap_points(vertices, edge_snap):
    """Tile-local (x, y) snap points around the rim of an arbitrary convex
    polygon given as an ordered list of (x, y) vertices (e.g. corner_xy()'s
    six hex corners, or a convex hull from segment_geometry.convex_hull()).

    `edge_snap` (>= 2) is the number of evenly-spaced points per edge,
    including both endpoints — e.g. 3 = the two vertices plus the exact
    midpoint. Vertices are shared between adjacent edges, so the returned
    list has len(vertices) * (edge_snap - 1) unique points, ordered
    edge-by-edge (each vertex followed by that edge's interior subdivisions
    before the next vertex)."""
    n = max(2, edge_snap)
    count = len(vertices)
    pts = []
    for i in range(count):
        a = vertices[i]
        b = vertices[(i + 1) % count]
        for k in range(n - 1):
            t = k / (n - 1)
            pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return pts


def edge_snap_points(diameter_mm, edge_snap):
    """Tile-local (x, y) snap points around the hex rim — see
    polygon_edge_snap_points() for the generic algorithm this wraps."""
    corners = [corner_xy(i, diameter_mm) for i in range(6)]
    return polygon_edge_snap_points(corners, edge_snap)


def point_in_polygon(x, y, vertices):
    """True if (x, y) lies inside (or on the boundary of) the convex polygon
    given as an ordered list of (x, y) vertices — a sign-consistency test,
    boundary-inclusive so a point that lands exactly on a vertex/edge still
    counts as inside."""
    count = len(vertices)
    sign = 0
    for i in range(count):
        ax, ay = vertices[i]
        bx, by = vertices[(i + 1) % count]
        cross = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        if cross > 1e-9:
            if sign < 0:
                return False
            sign = 1
        elif cross < -1e-9:
            if sign > 0:
                return False
            sign = -1
    return True


def point_in_hex(x, y, diameter_mm):
    """True if tile-local (x, y) lies inside (or on the boundary of) the hex
    with the given point-to-point diameter — see point_in_polygon() for the
    generic algorithm this wraps."""
    corners = [corner_xy(i, diameter_mm) for i in range(6)]
    return point_in_polygon(x, y, corners)


def hex_prism_verts_faces(diameter_mm, z_min, z_max):
    """Closed, 2-manifold vertical hex prism spanning [z_min, z_max]: the
    tile-local hexagon (per `corner_xy`) extruded straight up. Used as a
    boolean-tool solid to cut a mesh along one hex tile's boundary — see
    `operators._cut_terrain_by_hex`.

    Returns (verts, faces): 12 verts (6 bottom + 6 top, same XY per corner
    index), 8 faces (bottom cap, top cap, 6 side quads), all outward-facing.
    """
    corners = [corner_xy(i, diameter_mm) for i in range(6)]
    verts = ([(x, y, z_min) for x, y in corners]
             + [(x, y, z_max) for x, y in corners])
    bottom = tuple(range(6))            # winding gives outward (-Z) normal
    top = tuple(range(11, 5, -1))       # reversed winding gives outward (+Z)
    sides = [(i, i + 6, (i + 1) % 6 + 6, (i + 1) % 6) for i in range(6)]
    faces = [bottom, top] + sides
    return verts, faces


def find_tile(scene, q, r):
    """Return the HexFinity tile Object at (q, r) in `scene`'s map, or None.

    Reads scene.hexfinity_map.root_collection and scans its objects for the
    one whose hexfinity_tile.coord_q/coord_r matches. Linear scan — maps are
    small enough that a dict cache would be premature optimisation.
    """
    coll = scene.hexfinity_map.root_collection
    if coll is None:
        return None
    for obj in coll.objects:
        props = obj.hexfinity_tile
        if not props.is_generated:
            continue
        if props.coord_q == q and props.coord_r == r:
            return obj
    return None


def find_connected_component(nodes, start, epsilon=1e-3):
    """BFS over an undirected "shares a point" graph.

    `nodes`: {key: [(x, y), ...]} — every node's own representative points,
    in a common coordinate space. `start`: a key in `nodes`. Two nodes are
    connected if any one of node A's points lies within `epsilon` of any one
    of node B's points (squared-distance test). Returns the set of keys in
    the same connected component as `start` (always includes `start`).

    Plain BFS with a `visited` set — safe on graphs with cycles or branching
    (a node is only ever enqueued once), which callers with user-authored,
    potentially-looping connectivity (e.g. HexFinity's Path Feature linking)
    rely on for termination.
    """
    def _shares_point(pts_a, pts_b, eps2):
        for (ax, ay) in pts_a:
            for (bx, by) in pts_b:
                if (ax - bx) ** 2 + (ay - by) ** 2 <= eps2:
                    return True
        return False

    eps2 = epsilon * epsilon
    visited = {start}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        cur_pts = nodes[current]
        for key, pts in nodes.items():
            if key in visited:
                continue
            if _shares_point(cur_pts, pts, eps2):
                visited.add(key)
                frontier.append(key)
    return visited
