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


def edge_snap_points(diameter_mm, edge_snap):
    """Tile-local (x, y) snap points around the hex rim.

    `edge_snap` (>= 2) is the number of evenly-spaced points per edge,
    including both endpoints — e.g. 3 = the two P corners plus the exact
    midpoint. Corners are shared between adjacent edges, so the returned
    list has 6 * (edge_snap - 1) unique points, ordered corner-by-corner
    (each corner followed by that edge's interior subdivisions before the
    next corner)."""
    n = max(2, edge_snap)
    pts = []
    for i in range(6):
        a = corner_xy(i, diameter_mm)
        b = corner_xy((i + 1) % 6, diameter_mm)
        for k in range(n - 1):
            t = k / (n - 1)
            pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return pts


def point_in_hex(x, y, diameter_mm):
    """True if tile-local (x, y) lies inside (or on the boundary of) the hex
    with the given point-to-point diameter — a convex-polygon test over the
    six corner_xy() points, boundary-inclusive so a click that lands exactly
    on a P corner/rim still counts as inside."""
    corners = [corner_xy(i, diameter_mm) for i in range(6)]
    sign = 0
    for i in range(6):
        ax, ay = corners[i]
        bx, by = corners[(i + 1) % 6]
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
