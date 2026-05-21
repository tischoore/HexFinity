"""Pure-Python mesh construction for HexFinity tiles.

No `bpy` imports — this module is unit-testable in plain CPython.
All linear inputs are millimetres; output vertex coordinates are metres.
"""

import math


def build_hex_tile(
    diameter_mm,
    level_height_mm,
    base_thickness_mm,
    corner_levels,
    center_level,
    subdivisions,
):
    """Build a single HexFinity tile.

    Returns (verts, faces) where verts is a list of (x, y, z) tuples in metres
    and faces is a list of vertex-index tuples (triangles or quads).
    """
    if diameter_mm <= 0:
        raise ValueError(f"diameter_mm must be positive, got {diameter_mm}")
    if level_height_mm <= 0:
        raise ValueError(f"level_height_mm must be positive, got {level_height_mm}")
    if base_thickness_mm <= 0:
        raise ValueError(f"base_thickness_mm must be positive, got {base_thickness_mm}")
    if subdivisions < 0:
        raise ValueError(f"subdivisions must be >= 0, got {subdivisions}")
    if len(corner_levels) != 6:
        raise ValueError(f"corner_levels must have 6 entries, got {len(corner_levels)}")

    levels = tuple(max(0, int(L)) for L in corner_levels)
    N = subdivisions + 1
    R = diameter_mm / 2.0

    # P1 at 12 o'clock (+Y), P2..P6 clockwise viewed from above.
    corners_xy = []
    for i in range(6):
        angle = math.pi / 2.0 - i * (math.pi / 3.0)
        corners_xy.append((R * math.cos(angle), R * math.sin(angle)))

    corner_z = [base_thickness_mm + levels[i] * level_height_mm for i in range(6)]

    if center_level is not None:
        center_z = base_thickness_mm + max(0, int(center_level)) * level_height_mm
    else:
        center_z = sum(corner_z) / 6.0

    C_pos = (0.0, 0.0, center_z)
    corner_pos = [(corners_xy[i][0], corners_xy[i][1], corner_z[i]) for i in range(6)]

    verts_mm = []
    vert_index = {}

    def add_vert(key, pos):
        idx = vert_index.get(key)
        if idx is None:
            idx = len(verts_mm)
            vert_index[key] = idx
            verts_mm.append(pos)
        return idx

    faces = []

    # Top surface: 6 triangles (C, corner[i], corner[(i+1)%6]), each barycentrically
    # subdivided into N**2 sub-triangles. Shared spokes and rim points are deduplicated
    # via the (key -> index) map so neighbouring top triangles stitch automatically.
    def top_key(i, r, s):
        ip1 = (i + 1) % 6
        if r == 0:
            return ("center",)
        if r == N and s == 0:
            return ("corner", i)
        if r == N and s == N:
            return ("corner", ip1)
        if s == 0:
            return ("spoke", i, r)
        if s == r:
            return ("spoke", ip1, r)
        if r == N:
            return ("rim", i, s)
        return ("interior", i, r, s)

    def top_pos(i, r, s):
        ip1 = (i + 1) % 6
        wc = (N - r) / N
        wi = (r - s) / N
        wj = s / N
        x = wc * C_pos[0] + wi * corner_pos[i][0] + wj * corner_pos[ip1][0]
        y = wc * C_pos[1] + wi * corner_pos[i][1] + wj * corner_pos[ip1][1]
        z = wc * C_pos[2] + wi * corner_pos[i][2] + wj * corner_pos[ip1][2]
        return (x, y, z)

    for i in range(6):
        grid = {}
        for r in range(N + 1):
            for s in range(r + 1):
                grid[(r, s)] = add_vert(top_key(i, r, s), top_pos(i, r, s))
        for r in range(N):
            # Up sub-triangles (apex closer to C); wound for +Z normal.
            for s in range(r + 1):
                a = grid[(r, s)]
                b = grid[(r + 1, s)]
                c = grid[(r + 1, s + 1)]
                faces.append((a, c, b))
            # Down sub-triangles (apex away from C); wound for +Z normal.
            for s in range(r):
                a = grid[(r, s)]
                b = grid[(r, s + 1)]
                c = grid[(r + 1, s + 1)]
                faces.append((a, b, c))

    # Bottom perimeter ring + bottom centre — XY matches the top rim exactly so
    # side-wall quads connect 1:1, keeping the mesh manifold.
    def bot_key(i, s):
        if s == 0:
            return ("bcorner", i)
        if s == N:
            return ("bcorner", (i + 1) % 6)
        return ("brim", i, s)

    bot_center_idx = add_vert(("bcenter",), (0.0, 0.0, 0.0))

    for i in range(6):
        ip1 = (i + 1) % 6
        for s in range(N + 1):
            wi = (N - s) / N
            wj = s / N
            x = wi * corner_pos[i][0] + wj * corner_pos[ip1][0]
            y = wi * corner_pos[i][1] + wj * corner_pos[ip1][1]
            add_vert(bot_key(i, s), (x, y, 0.0))

    def top_rim_key(i, s):
        if s == 0:
            return ("corner", i)
        if s == N:
            return ("corner", (i + 1) % 6)
        return ("rim", i, s)

    # Side walls (quads, wound for outward-facing normal).
    for i in range(6):
        for s in range(N):
            top_a = vert_index[top_rim_key(i, s)]
            top_b = vert_index[top_rim_key(i, s + 1)]
            bot_a = vert_index[bot_key(i, s)]
            bot_b = vert_index[bot_key(i, s + 1)]
            faces.append((top_a, bot_a, bot_b, top_b))

    # Bottom fan from bcenter to perimeter (wound for -Z normal).
    for i in range(6):
        for s in range(N):
            bot_a = vert_index[bot_key(i, s)]
            bot_b = vert_index[bot_key(i, s + 1)]
            faces.append((bot_center_idx, bot_a, bot_b))

    verts_m = [(x / 1000.0, y / 1000.0, z / 1000.0) for (x, y, z) in verts_mm]
    return verts_m, faces
