"""Pure-Python mesh construction for HexFinity tiles.

No `bpy` imports — this module is unit-testable in plain CPython.
All linear inputs and output vertex coordinates are in millimetres.

Top surface: six bicubic Hermite Coons patches, one per `(C, Pi, Pi+1)` region.
Each patch has its `u=0` boundary collapsed to the apex `C`. Boundary curves
along internal spokes and cross-boundary tangent fields are shared between
neighbouring patches by construction, giving strict G1 across every internal
spoke for any corner heights. The cross-rim tangent is horizontal, so tiles
placed side-by-side join with a continuous tangent plane.
"""

import math


# Inter-tile interlock geometry. Each side carries one rectangular tab sticking
# radially outward and one matching cavity into the side. Neighbouring tiles
# mate because tab and hole sit at symmetric positions on the shared edge.
TAB_WIDTH_MM = 10.0              # along the side
TAB_HEIGHT_MM = 8.0              # vertical (z)
TAB_DEPTH_MM = 10.0              # radially outward
TAB_OFFSET_FROM_CORNER_MM = 10.0  # tab is this far from the next corner;
                                  # hole is this far from the previous corner.
TAB_HOLE_TOLERANCE_MM = 0.2      # hole is +TOL in every axis vs tab so tiles slide together.


def _hermite_basis(t):
    """Cubic Hermite basis: returns (H0, H1, G0, G1).

    H0/H1 interpolate values at t=0/t=1; G0/G1 interpolate first derivatives.
    """
    t2 = t * t
    t3 = t2 * t
    H0 = 2.0 * t3 - 3.0 * t2 + 1.0
    H1 = -2.0 * t3 + 3.0 * t2
    G0 = t3 - 2.0 * t2 + t
    G1 = t3 - t2
    return (H0, H1, G0, G1)


def _patch_data(i, C_pos, corner_pos, spoke_normals, rim_normals,
                alpha, beta, gamma):
    """Per-patch boundary endpoints and cross-boundary tangent vectors.

    All tangent vectors have z=0 by construction (horizontal in XY); that is
    what enforces horizontal tangent planes at the apex, rim, and corners.
    """
    ip1 = (i + 1) % 6
    Pi = corner_pos[i]
    Pip1 = corner_pos[ip1]
    n_spoke_i = spoke_normals[i]
    n_spoke_ip1 = spoke_normals[ip1]
    n_rim_i = rim_normals[i]

    # Apex radial peel-off endpoints (Pi - C and Pip1 - C, with z zeroed).
    Tu0_at_v0 = (alpha * (Pi[0] - C_pos[0]),
                 alpha * (Pi[1] - C_pos[1]),
                 0.0)
    Tu0_at_v1 = (alpha * (Pip1[0] - C_pos[0]),
                 alpha * (Pip1[1] - C_pos[1]),
                 0.0)

    # Outward rim tangent — constant in v.
    Tu1 = (beta * n_rim_i[0], beta * n_rim_i[1], 0.0)

    # Cross-spoke tangents — constant in u, shared per-spoke with neighbours.
    Tv0 = (gamma * n_spoke_i[0], gamma * n_spoke_i[1], 0.0)
    Tv1 = (gamma * n_spoke_ip1[0], gamma * n_spoke_ip1[1], 0.0)

    return {
        "C": C_pos,
        "Pi": Pi,
        "Pip1": Pip1,
        "Tu0_at_v0": Tu0_at_v0,
        "Tu0_at_v1": Tu0_at_v1,
        "Tu1": Tu1,
        "Tv0": Tv0,
        "Tv1": Tv1,
    }


def _eval_coons(u, v, pd):
    """Evaluate the Coons patch S(u,v) = F_u + F_v - B at (u, v)."""
    C = pd["C"]
    Pi = pd["Pi"]
    Pip1 = pd["Pip1"]
    Tu1 = pd["Tu1"]
    Tv0 = pd["Tv0"]
    Tv1 = pd["Tv1"]
    Tu0_v0 = pd["Tu0_at_v0"]
    Tu0_v1 = pd["Tu0_at_v1"]

    H0u, H1u, G0u, G1u = _hermite_basis(u)
    H0v, H1v, G0v, G1v = _hermite_basis(v)

    # Boundary curves at this (u,v).
    # c0(v) = C (degenerate apex), c1(v) = (1-v)*Pi + v*Pip1.
    c1v = ((1.0 - v) * Pi[0] + v * Pip1[0],
           (1.0 - v) * Pi[1] + v * Pip1[1],
           (1.0 - v) * Pi[2] + v * Pip1[2])
    # d0(u) = (1-u)*C + u*Pi, d1(u) = (1-u)*C + u*Pip1.
    d0u = ((1.0 - u) * C[0] + u * Pi[0],
           (1.0 - u) * C[1] + u * Pi[1],
           (1.0 - u) * C[2] + u * Pi[2])
    d1u = ((1.0 - u) * C[0] + u * Pip1[0],
           (1.0 - u) * C[1] + u * Pip1[1],
           (1.0 - u) * C[2] + u * Pip1[2])

    # Cross-boundary tangent fields at this (u,v).
    # Tu0(v) is linear in v; Tu1(v), Tv0(u), Tv1(u) are constant.
    Tu0v = ((1.0 - v) * Tu0_v0[0] + v * Tu0_v1[0],
            (1.0 - v) * Tu0_v0[1] + v * Tu0_v1[1],
            0.0)

    # F_u(u,v) = H0(u)*c0 + H1(u)*c1(v) + G0(u)*Tu0(v) + G1(u)*Tu1(v)
    Fu_x = H0u * C[0] + H1u * c1v[0] + G0u * Tu0v[0] + G1u * Tu1[0]
    Fu_y = H0u * C[1] + H1u * c1v[1] + G0u * Tu0v[1] + G1u * Tu1[1]
    Fu_z = H0u * C[2] + H1u * c1v[2] + G0u * Tu0v[2] + G1u * Tu1[2]

    # F_v(u,v) = H0(v)*d0(u) + H1(v)*d1(u) + G0(v)*Tv0(u) + G1(v)*Tv1(u)
    Fv_x = H0v * d0u[0] + H1v * d1u[0] + G0v * Tv0[0] + G1v * Tv1[0]
    Fv_y = H0v * d0u[1] + H1v * d1u[1] + G0v * Tv0[1] + G1v * Tv1[1]
    Fv_z = H0v * d0u[2] + H1v * d1u[2] + G0v * Tv0[2] + G1v * Tv1[2]

    # B(u,v) = sum_{a,b} M[a][b] * bu[a] * bv[b]; twists = 0.
    # Rows of M: 0→S(0,*), 1→S(1,*), 2→Tu0(*), 3→Tu1.
    # Cols of M: 0→S(*,0), 1→S(*,1), 2→Tv0, 3→Tv1.
    zero = (0.0, 0.0, 0.0)
    M = (
        (C,         C,         Tv0,     Tv1),
        (Pi,        Pip1,      Tv0,     Tv1),
        (Tu0_v0,    Tu0_v1,    zero,    zero),
        (Tu1,       Tu1,       zero,    zero),
    )
    bu = (H0u, H1u, G0u, G1u)
    bv = (H0v, H1v, G0v, G1v)
    Bx = By = Bz = 0.0
    for a in range(4):
        row = M[a]
        bua = bu[a]
        for b in range(4):
            coef = bua * bv[b]
            m = row[b]
            Bx += coef * m[0]
            By += coef * m[1]
            Bz += coef * m[2]

    return (Fu_x + Fv_x - Bx,
            Fu_y + Fv_y - By,
            Fu_z + Fv_z - Bz)


def clamp_center_to_hexagon(x_mm, y_mm, diameter_mm, safety_mm=1.0):
    """Project (x_mm, y_mm) into the open hexagon with a safety buffer.

    Constraint: n_i · p <= apothem - safety_mm for each of the six outward
    rim unit normals, where apothem = (diameter / 2) · √3 / 2. Two passes
    handle corner regions where two adjacent half-planes are violated.
    """
    apothem = (diameter_mm / 2.0) * math.sqrt(3.0) / 2.0
    limit = apothem - safety_mm
    x, y = x_mm, y_mm
    for _ in range(2):
        for i in range(6):
            theta = math.pi / 6.0 - i * (math.pi / 3.0)
            nx = math.cos(theta)
            ny = math.sin(theta)
            d = nx * x + ny * y
            if d > limit:
                excess = d - limit
                x -= excess * nx
                y -= excess * ny
    return x, y


def build_hex_tile(
    diameter_mm,
    level_height_mm,
    base_thickness_mm,
    corner_levels,
    center_level,
    subdivisions,
    center_xy=(0.0, 0.0),
):
    """Build a single HexFinity tile.

    Returns (verts, faces) where verts is a list of (x, y, z) tuples in
    millimetres and faces is a list of vertex-index tuples (triangles or
    quads). Default STL export from Blender writes raw vertex values, so
    keeping the mesh in mm makes the exported file imports at true mm scale.
    """
    if diameter_mm <= 0:
        raise ValueError(f"diameter_mm must be positive, got {diameter_mm}")
    if level_height_mm <= 0:
        raise ValueError(f"level_height_mm must be positive, got {level_height_mm}")
    if base_thickness_mm < TAB_HEIGHT_MM + TAB_HOLE_TOLERANCE_MM:
        raise ValueError(
            f"base_thickness_mm must be at least "
            f"{TAB_HEIGHT_MM + TAB_HOLE_TOLERANCE_MM} mm to fit the tab/hole "
            f"interlock, got {base_thickness_mm}"
        )
    if subdivisions < 0:
        raise ValueError(f"subdivisions must be >= 0, got {subdivisions}")
    if len(corner_levels) != 6:
        raise ValueError(f"corner_levels must have 6 entries, got {len(corner_levels)}")
    # Diameter must be wide enough that the tab and hole on a single side don't
    # overlap and leave a printable gap of solid material between them. The
    # hole spans [OFFSET - TOL/2, OFFSET + TAB_WIDTH + TOL/2] and the tab spans
    # [side_len - OFFSET - TAB_WIDTH, side_len - OFFSET]. Require >= 0.1 mm gap.
    _side_len = diameter_mm / 2.0
    _gap = _side_len - 2.0 * TAB_OFFSET_FROM_CORNER_MM - 2.0 * TAB_WIDTH_MM - TAB_HOLE_TOLERANCE_MM / 2.0
    if _gap < 0.1:
        raise ValueError(
            f"diameter_mm={diameter_mm} too small for tab/hole interlock: "
            f"side length {_side_len:.3f} mm leaves a gap of {_gap:.3f} mm "
            f"between hole and tab (need >= 0.1 mm)"
        )

    levels = tuple(max(0, int(L)) for L in corner_levels)
    N = subdivisions + 1
    R = diameter_mm / 2.0
    apothem = R * math.sqrt(3.0) / 2.0  # rim midpoint distance from origin

    # Flat-top hex: P1 at upper-right (1 o'clock), P2..P6 clockwise viewed
    # from above. P2 sits at +X, P3 lower-right, P4 lower-left, P5 at -X,
    # P6 upper-left.
    corners_xy = []
    for i in range(6):
        angle = math.pi / 3.0 - i * (math.pi / 3.0)
        corners_xy.append((R * math.cos(angle), R * math.sin(angle)))

    corner_z = [base_thickness_mm + levels[i] * level_height_mm for i in range(6)]

    if center_level is not None:
        center_z = base_thickness_mm + max(0, int(center_level)) * level_height_mm
    else:
        center_z = sum(corner_z) / 6.0

    C_pos = (float(center_xy[0]), float(center_xy[1]), center_z)
    corner_pos = [(corners_xy[i][0], corners_xy[i][1], corner_z[i]) for i in range(6)]

    # Per-spoke and per-rim unit XY normals (shared between adjacent patches).
    spoke_normals = []
    for i in range(6):
        # rotate_90_ccw of unit(Pi - C)_xy0 = (-uy/mag, ux/mag, 0)
        ux = corner_pos[i][0] - C_pos[0]
        uy = corner_pos[i][1] - C_pos[1]
        mag = math.hypot(ux, uy)
        spoke_normals.append((-uy / mag, ux / mag, 0.0))

    rim_normals = []
    for i in range(6):
        ip1 = (i + 1) % 6
        mx = 0.5 * (corner_pos[i][0] + corner_pos[ip1][0])
        my = 0.5 * (corner_pos[i][1] + corner_pos[ip1][1])
        mag = math.hypot(mx, my)
        rim_normals.append((mx / mag, my / mag, 0.0))

    # Tangent magnitudes. Only their consistency between patches/tiles matters
    # for G1; these defaults give visually balanced curvature.
    alpha = 1.0
    beta = apothem
    gamma = apothem

    patches = [
        _patch_data(i, C_pos, corner_pos, spoke_normals, rim_normals,
                    alpha, beta, gamma)
        for i in range(6)
    ]

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

    # Top surface: six bicubic Hermite Coons patches.
    def top_key(i, u_idx, v_idx):
        ip1 = (i + 1) % 6
        if u_idx == 0:
            return ("center",)
        if u_idx == N and v_idx == 0:
            return ("corner", i)
        if u_idx == N and v_idx == N:
            return ("corner", ip1)
        if v_idx == 0:
            return ("spoke", i, u_idx)
        if v_idx == N:
            return ("spoke", ip1, u_idx)
        if u_idx == N:
            return ("rim", i, v_idx)
        return ("interior", i, u_idx, v_idx)

    for i in range(6):
        pd = patches[i]
        grid = {}
        for u_idx in range(N + 1):
            u = u_idx / N
            for v_idx in range(N + 1):
                v = v_idx / N
                pos = _eval_coons(u, v, pd)
                grid[(u_idx, v_idx)] = add_vert(top_key(i, u_idx, v_idx), pos)

        # Apex triangle fan at u_idx=0 (degenerate row collapsed to center).
        # Winding: C → grid[1,v+1] → grid[1,v] gives +Z normal.
        apex = grid[(0, 0)]
        for v_idx in range(N):
            faces.append((apex, grid[(1, v_idx + 1)], grid[(1, v_idx)]))

        # Quad cells for u_idx >= 1. Winding: A,D,C,B (= reverse of parametric
        # CCW) for +Z normal viewed from above.
        for u_idx in range(1, N):
            for v_idx in range(N):
                A = grid[(u_idx, v_idx)]
                B = grid[(u_idx + 1, v_idx)]
                Cq = grid[(u_idx + 1, v_idx + 1)]
                D = grid[(u_idx, v_idx + 1)]
                faces.append((A, D, Cq, B))

    # Bottom of the tile carries inter-tile tab/hole interlocks (see module
    # constants). Side-wall, base plate and per-side tab/cavity geometry are
    # all driven off four u-coordinate breakpoints per side. The top surface
    # built above is unaffected; only the bottom rim is densified.
    side_len = R
    u_hole_lo = TAB_OFFSET_FROM_CORNER_MM - TAB_HOLE_TOLERANCE_MM / 2.0
    u_hole_hi = TAB_OFFSET_FROM_CORNER_MM + TAB_WIDTH_MM + TAB_HOLE_TOLERANCE_MM / 2.0
    u_tab_lo = side_len - TAB_OFFSET_FROM_CORNER_MM - TAB_WIDTH_MM
    u_tab_hi = side_len - TAB_OFFSET_FROM_CORNER_MM
    hole_top_z = TAB_HEIGHT_MM + TAB_HOLE_TOLERANCE_MM
    hole_depth = TAB_DEPTH_MM + TAB_HOLE_TOLERANCE_MM

    def top_rim_key(i, s):
        if s == 0:
            return ("corner", i)
        if s == N:
            return ("corner", (i + 1) % 6)
        return ("rim", i, s)

    # Per-side outward and along-side unit vectors. Each side runs from P_i to
    # P_(i+1) clockwise viewed from +Z; outward = rim_normals[i] (toward +Y of
    # the hex centre); side_dir advances u from 0 to side_len along the rim.
    side_dirs = []
    for i in range(6):
        ip1 = (i + 1) % 6
        dx = corner_pos[ip1][0] - corner_pos[i][0]
        dy = corner_pos[ip1][1] - corner_pos[i][1]
        mag = math.hypot(dx, dy)
        side_dirs.append((dx / mag, dy / mag, 0.0))

    bot_center_idx = add_vert(("bcenter",), (0.0, 0.0, 0.0))

    def point_on_side(i, u, z):
        ip1 = (i + 1) % 6
        t = u / side_len
        return (corner_pos[i][0] + t * (corner_pos[ip1][0] - corner_pos[i][0]),
                corner_pos[i][1] + t * (corner_pos[ip1][1] - corner_pos[i][1]),
                z)

    # ----- Bottom rim corners + 4 breakpoints per side at z=0 on the rim -----
    for i in range(6):
        ip1 = (i + 1) % 6
        add_vert(("bcorner", i), (corner_pos[i][0], corner_pos[i][1], 0.0))
        add_vert(("bcorner", ip1), (corner_pos[ip1][0], corner_pos[ip1][1], 0.0))
        for label, u in (("hole_lo", u_hole_lo), ("hole_hi", u_hole_hi),
                         ("tab_lo", u_tab_lo), ("tab_hi", u_tab_hi)):
            add_vert(("bbreak", i, label), point_on_side(i, u, 0.0))

    # ----- Tab geometry: 6 new verts (2 share bbreak/tab_*) + 5 faces per side
    for i in range(6):
        outward = rim_normals[i]
        inner_lo = point_on_side(i, u_tab_lo, 0.0)
        inner_hi = point_on_side(i, u_tab_hi, 0.0)
        ox = TAB_DEPTH_MM * outward[0]
        oy = TAB_DEPTH_MM * outward[1]
        H = TAB_HEIGHT_MM

        # Two shared bottom-inner verts (already at bbreak/tab_lo, tab_hi).
        v_inner_lo_bot = vert_index[("bbreak", i, "tab_lo")]
        v_inner_hi_bot = vert_index[("bbreak", i, "tab_hi")]
        # Six new verts.
        v_inner_lo_top = add_vert(("tab_inner_top", i, "lo"),
                                   (inner_lo[0], inner_lo[1], H))
        v_inner_hi_top = add_vert(("tab_inner_top", i, "hi"),
                                   (inner_hi[0], inner_hi[1], H))
        v_outer_lo_bot = add_vert(("tab_outer_bot", i, "lo"),
                                   (inner_lo[0] + ox, inner_lo[1] + oy, 0.0))
        v_outer_hi_bot = add_vert(("tab_outer_bot", i, "hi"),
                                   (inner_hi[0] + ox, inner_hi[1] + oy, 0.0))
        v_outer_lo_top = add_vert(("tab_outer_top", i, "lo"),
                                   (inner_lo[0] + ox, inner_lo[1] + oy, H))
        v_outer_hi_top = add_vert(("tab_outer_top", i, "hi"),
                                   (inner_hi[0] + ox, inner_hi[1] + oy, H))

        # 5 faces — top, outer (front), bottom, lo end cap, hi end cap.
        # Wound for outward normals (verified topologically; visual normal flip
        # would be a single mesh.flip_normals away if any face came out wrong).
        faces.append((v_inner_lo_top, v_inner_hi_top, v_outer_hi_top, v_outer_lo_top))  # top +Z
        faces.append((v_outer_lo_bot, v_outer_hi_bot, v_outer_hi_top, v_outer_lo_top))  # outer +rim
        faces.append((v_inner_lo_bot, v_outer_lo_bot, v_outer_hi_bot, v_inner_hi_bot))  # bottom -Z
        faces.append((v_inner_lo_bot, v_inner_lo_top, v_outer_lo_top, v_outer_lo_bot))  # lo cap -side
        faces.append((v_inner_hi_bot, v_outer_hi_bot, v_outer_hi_top, v_inner_hi_top))  # hi cap +side

    # ----- Hole cavity: 6 new verts + 4 faces per side (no floor, no front) --
    for i in range(6):
        outward = rim_normals[i]
        outer_lo = point_on_side(i, u_hole_lo, 0.0)
        outer_hi = point_on_side(i, u_hole_hi, 0.0)
        ix = -hole_depth * outward[0]
        iy = -hole_depth * outward[1]
        Hz = hole_top_z

        v_outer_lo_bot = vert_index[("bbreak", i, "hole_lo")]
        v_outer_hi_bot = vert_index[("bbreak", i, "hole_hi")]
        v_outer_lo_top = add_vert(("hole_outer_top", i, "lo"),
                                   (outer_lo[0], outer_lo[1], Hz))
        v_outer_hi_top = add_vert(("hole_outer_top", i, "hi"),
                                   (outer_hi[0], outer_hi[1], Hz))
        v_inner_lo_bot = add_vert(("hole_inner_bot", i, "lo"),
                                   (outer_lo[0] + ix, outer_lo[1] + iy, 0.0))
        v_inner_hi_bot = add_vert(("hole_inner_bot", i, "hi"),
                                   (outer_hi[0] + ix, outer_hi[1] + iy, 0.0))
        v_inner_lo_top = add_vert(("hole_inner_top", i, "lo"),
                                   (outer_lo[0] + ix, outer_lo[1] + iy, Hz))
        v_inner_hi_top = add_vert(("hole_inner_top", i, "hi"),
                                   (outer_hi[0] + ix, outer_hi[1] + iy, Hz))

        # 4 faces — cavity ceiling, back wall, lo end cap, hi end cap. The
        # outward (rim-facing) opening and the bottom are both absent.
        faces.append((v_outer_lo_top, v_outer_hi_top, v_inner_hi_top, v_inner_lo_top))   # ceiling -Z
        faces.append((v_inner_lo_bot, v_inner_lo_top, v_inner_hi_top, v_inner_hi_bot))   # back wall +outward
        faces.append((v_outer_lo_bot, v_outer_lo_top, v_inner_lo_top, v_inner_lo_bot))   # lo cap +side
        faces.append((v_outer_hi_bot, v_inner_hi_bot, v_inner_hi_top, v_outer_hi_top))   # hi cap -side

    # ----- Side wall: one n-gon per side, walking the wall boundary CCW from
    # outside the hex (top rim left→right, then bottom rim right→left with
    # rectangular detours UP around the tab and hole openings).
    for i in range(6):
        ip1 = (i + 1) % 6
        ngon = []
        for s in range(N + 1):
            ngon.append(vert_index[top_rim_key(i, s)])
        ngon.append(vert_index[("bcorner", ip1)])
        ngon.append(vert_index[("bbreak", i, "tab_hi")])
        ngon.append(vert_index[("tab_inner_top", i, "hi")])
        ngon.append(vert_index[("tab_inner_top", i, "lo")])
        ngon.append(vert_index[("bbreak", i, "tab_lo")])
        ngon.append(vert_index[("bbreak", i, "hole_hi")])
        ngon.append(vert_index[("hole_outer_top", i, "hi")])
        ngon.append(vert_index[("hole_outer_top", i, "lo")])
        ngon.append(vert_index[("bbreak", i, "hole_lo")])
        ngon.append(vert_index[("bcorner", i)])
        faces.append(tuple(ngon))

    # ----- Bottom plate: fan from bcenter to a reduced perimeter walk plus
    # two ear triangles per side. The polygon "sector minus cavity" is not
    # star-shaped from bcenter — straight lines from bcenter to either rim
    # hole corner (hole_lo / hole_hi) cross the cavity interior at z=0.
    # The ears cover the rim strips flanking each cavity without intruding
    # into the cavity footprint.
    for i in range(6):
        ip1 = (i + 1) % 6
        bcorner_lo = vert_index[("bcorner", i)]
        bcorner_hi = vert_index[("bcorner", ip1)]
        hole_lo_rim = vert_index[("bbreak", i, "hole_lo")]
        hole_hi_rim = vert_index[("bbreak", i, "hole_hi")]
        hole_inner_lo = vert_index[("hole_inner_bot", i, "lo")]
        hole_inner_hi = vert_index[("hole_inner_bot", i, "hi")]
        tab_lo_rim = vert_index[("bbreak", i, "tab_lo")]
        tab_hi_rim = vert_index[("bbreak", i, "tab_hi")]
        faces.append((bcorner_lo, hole_lo_rim, hole_inner_lo))
        faces.append((hole_inner_hi, hole_hi_rim, tab_lo_rim))
        walk = (
            bcorner_lo,
            hole_inner_lo,
            hole_inner_hi,
            tab_lo_rim,
            tab_hi_rim,
            bcorner_hi,
        )
        for k in range(len(walk) - 1):
            faces.append((bot_center_idx, walk[k], walk[k + 1]))

    return verts_mm, faces
