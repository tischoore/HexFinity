"""Pure-Python mesh construction for HexFinity tiles.

No `bpy` imports — this module is unit-testable in plain CPython.
All linear inputs and output vertex coordinates are in millimetres.

Top surface: Loop subdivision over a 13-vertex control mesh (centre `C`,
six rim corners `P1..P6`, six auto-derived inner-ring vertices `Q1..Q6`).
The six rim edges are tagged sharp so they stay perfectly straight under
subdivision (mandatory for the side wall to attach and for adjacent tiles
to meet without gaps). Everything else smooths via Loop's stencils, which
naturally damps a centre displacement into a radial bump and eliminates
the per-spoke creases that the previous Coons-patch construction had.
"""

import math

try:
    # Package import path used by the Blender extension.
    from .subdivision import subdivide_loop, linear_midpoint_subdivide
    from . import procedural_surfaces
except ImportError:
    # Flat import path used by the unit-test conftest (adds hexfinity/ to
    # sys.path so the bpy-free modules can be imported without the package).
    from subdivision import subdivide_loop, linear_midpoint_subdivide
    import procedural_surfaces


# Default width (mm) of the band over which a procedural surface fades to flat at
# the hex rim, so shared edges/corners keep their exact interlock heights.
SURFACE_RIM_FALLOFF_MM = 15.0


# Inter-tile interlock geometry. Each side carries one rectangular tab sticking
# radially outward and one matching cavity into the side. Neighbouring tiles
# mate because tab and hole sit at symmetric positions on the shared edge.
TAB_WIDTH_MM = 10.0              # along the side
TAB_HEIGHT_MM = 8.0              # vertical (z)
TAB_DEPTH_MM = 10.0              # radially outward
TAB_OFFSET_FROM_CORNER_MM = 10.0  # tab is this far from the next corner;
                                  # hole is this far from the previous corner.
TAB_HOLE_TOLERANCE_MM = 0.2      # hole is +TOL in every axis vs tab so tiles slide together.
TAB_FILLET_MM = 4.0              # radius rounding the two outer (leading) vertical
                                 # edges of each tab so tiles mate more easily; the
                                 # square hole is left sharp.
TAB_FILLET_SEGMENTS = 3          # arc tessellation per rounded outer corner.

# Inner-ring control vertex placement. Qi sits between C and the midpoint of
# rim segment Pi→Pi+1, at this fraction of the way out. 0.5 keeps the Q ring
# concentric and reasonably tight to C; tuning this trades dome breadth for
# spoke flatness.
INNER_RING_FACTOR = 0.5


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


def rim_edge_distance(x_mm, y_mm, diameter_mm):
    """Minimum perpendicular distance from a local-XY point to the hex rim.

    Returns the distance (mm) from (x_mm, y_mm) to the nearest of the six rim
    edges, measured in the tile's local frame (hex centred on the origin, same
    flat-top orientation as `build_hex_tile`). Each edge lies at the apothem
    along its outward unit normal n_i, so the signed distance to edge i is
    `apothem - n_i·p`; the rim distance is the smallest of those six.

    apothem at the centre, 0 on an edge, and symmetric across the hexagon. The
    terrain brush uses this to damp its displacement to ~0 near the rim so the
    straight edges and shared corners stay put. bpy-free for unit testing.
    """
    apothem = (diameter_mm / 2.0) * math.sqrt(3.0) / 2.0
    best = None
    for i in range(6):
        theta = math.pi / 6.0 - i * (math.pi / 3.0)
        d = apothem - (math.cos(theta) * x_mm + math.sin(theta) * y_mm)
        if best is None or d < best:
            best = d
    return best


def top_vertex_count(smoothness_passes, resample_density=0):
    """Number of top-surface vertices `build_hex_tile` produces for these passes.

    The top surface is the subdivision of a fixed 13-vertex / 30-edge / 18-face
    control mesh. Both Loop and linear-midpoint subdivision are topologically
    identical (one new vertex per edge, every triangle → 4), so each pass maps
    (V, E, F) → (V + E, 2E + 3F, 4F). The vertex count therefore depends only on
    the total pass count, never on the z-heights — which is exactly what makes a
    `float[num_top]` displacement array a stable, topology-keyed layer.

    Pinned to the real builder by `test_top_vertex_count_matches_builder`.
    """
    V, E, F = 13, 30, 18
    for _ in range(smoothness_passes + resample_density):
        V, E, F = V + E, 2 * E + 3 * F, 4 * F
    return V


def effective_resample(global_density, local_subdiv):
    """Total linear-midpoint passes for a tile: the map-wide global plus the
    tile's own local subdivision (clamped to >= 0).

    bpy-free so the per-tile density semantics are unit-testable and reusable by
    other geometric operations. Feeds straight into `build_hex_tile`'s
    `resample_density` (and therefore `top_vertex_count`).
    """
    return int(global_density) + max(0, int(local_subdiv))


def _surface_offset_for_regions(x, y, regions, origin_xy, seed):
    """Sum the procedural-surface z-offset (mm) of every region at (x, y).

    Each region contributes `region_mask · surface_offset`; a region with no
    polygon covers the whole tile (mask 1). The rim fade is applied by the
    caller. bpy-free helper kept here so the builder owns rim/region blending.
    """
    total = 0.0
    for reg in regions:
        poly = reg.get("polygon")
        if poly:
            mask = procedural_surfaces.region_mask(
                x, y, poly, reg.get("mask_falloff_mm", 0.0))
            if mask <= 0.0:
                continue
        else:
            mask = 1.0
        total += mask * procedural_surfaces.surface_offset(
            x, y,
            surface_type=reg.get("surface_type", "NONE"),
            feature_mm=reg.get("feature_mm", 0.0),
            depth_mm=reg.get("depth_mm", 0.0),
            regularity=reg.get("regularity", 0.5),
            seed=reg.get("seed", seed),
            origin_xy=origin_xy,
            direction_rad=reg.get("direction_rad", 0.0),
        )
    return total


def resolve_center_z(corner_z, center_level, base_thickness_mm, level_height_mm):
    """Z (mm) of the centre apex control point.

    When `center_level` is None the centre is **not** overridden, so it follows
    the corners — the mean of the six corner Zs — and is therefore recalculated
    on every rebuild (raising or lowering corners moves the centre with them).
    When `center_level` is an int the centre is pinned to that absolute level and
    **ignores** corner changes. This encodes the "recalculate the centre height
    unless it is set to ignore (overridden)" contract; kept bpy-free so it can be
    unit-tested without Blender.
    """
    if center_level is not None:
        return base_thickness_mm + max(0, int(center_level)) * level_height_mm
    return sum(corner_z) / 6.0


def build_hex_tile(
    diameter_mm,
    level_height_mm,
    base_thickness_mm,
    corner_levels,
    center_level,
    smoothness_passes,
    resample_density=0,
    center_xy=(0.0, 0.0),
    dome_area=INNER_RING_FACTOR,
    dome_damping=2.0 / 3.0,
    top_displacement=None,
    surface_regions=None,
    surface_origin_xy=(0.0, 0.0),
    surface_seed=0,
    surface_rim_falloff_mm=SURFACE_RIM_FALLOFF_MM,
):
    """Build a single HexFinity tile.

    Returns (verts, faces) where verts is a list of (x, y, z) tuples in
    millimetres and faces is a list of vertex-index tuples (triangles or
    quads). Default STL export from Blender writes raw vertex values, so
    keeping the mesh in mm makes the exported file imports at true mm scale.

    `smoothness_passes` runs Loop subdivision (shape detail and smoothness);
    `resample_density` then runs linear midpoint subdivision passes that
    increase polycount via chord midpoints without further smoothing.

    `top_displacement`, when given, is a per-top-vertex z-offset (mm) painted by
    the terrain brush. Its length must equal `top_vertex_count(smoothness_passes,
    resample_density)` (the number of top verts, which are registered first at
    indices `0 .. num_top-1`); a mismatch is ignored so a stale array from a
    different subdivision is dropped rather than misapplied. Each offset is added
    to its top vert's z and the result is clamped to `>= base_thickness_mm` so a
    deep "lower" stroke can never punch the top through the base and invert the
    side walls. The displacement is z-only and topology-preserving, so the
    `check_manifold()` run by the caller still validates the painted mesh.

    `surface_regions`, when given, is a list of procedural-surface regions, each a
    dict: `surface_type`, `feature_mm`, `depth_mm`, `regularity`, `direction_rad`,
    optional `polygon` (list of (x, y) tile-local mm — omitted/empty = whole tile),
    optional `mask_falloff_mm` (boundary blend band), and optional `seed`. Each
    top vert's z gets `Σ region_mask · surface_offset` (see `procedural_surfaces`)
    added, scaled by a rim fade so the texture vanishes within
    `surface_rim_falloff_mm` of the rim (seams stay flat for interlock). Sampled in
    GLOBAL coords (`surface_origin_xy` = tile world XY) so patterns flow across
    seams. Like `top_displacement` this is z-only and clamped to `base_thickness_mm`,
    so the manifold guarantee holds.
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
    if smoothness_passes < 0:
        raise ValueError(
            f"smoothness_passes must be >= 0, got {smoothness_passes}"
        )
    if resample_density < 0:
        raise ValueError(
            f"resample_density must be >= 0, got {resample_density}"
        )
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
    # Outer-corner fillet must fit inside the tab footprint: two fillets can't
    # eat more than the tab width, and one can't exceed the tab depth.
    if TAB_FILLET_MM >= TAB_WIDTH_MM / 2.0 or TAB_FILLET_MM >= TAB_DEPTH_MM:
        raise ValueError(
            f"TAB_FILLET_MM={TAB_FILLET_MM} too large for tab "
            f"{TAB_WIDTH_MM}x{TAB_DEPTH_MM} mm footprint"
        )

    levels = tuple(max(0, int(L)) for L in corner_levels)
    R = diameter_mm / 2.0

    # Flat-top hex: P1 at upper-right (1 o'clock), P2..P6 clockwise viewed
    # from above. P2 sits at +X, P3 lower-right, P4 lower-left, P5 at -X,
    # P6 upper-left.
    corners_xy = []
    for i in range(6):
        angle = math.pi / 3.0 - i * (math.pi / 3.0)
        corners_xy.append((R * math.cos(angle), R * math.sin(angle)))

    corner_z = [base_thickness_mm + levels[i] * level_height_mm for i in range(6)]

    center_z = resolve_center_z(corner_z, center_level,
                                base_thickness_mm, level_height_mm)

    C_pos = (float(center_xy[0]), float(center_xy[1]), center_z)
    corner_pos = [(corners_xy[i][0], corners_xy[i][1], corner_z[i]) for i in range(6)]

    # Per-rim outward unit XY normals — still needed below for tab/hole
    # placement and the side-wall n-gon construction.
    rim_normals = []
    for i in range(6):
        ip1 = (i + 1) % 6
        mx = 0.5 * (corner_pos[i][0] + corner_pos[ip1][0])
        my = 0.5 * (corner_pos[i][1] + corner_pos[ip1][1])
        mag = math.hypot(mx, my)
        rim_normals.append((mx / mag, my / mag, 0.0))

    # ---- 13-vert control mesh (C + P1..P6 + Q1..Q6) ----------------------
    # Qi.xy = C.xy + dome_area · (midpoint(Pi, Pi+1) − C.xy)
    # Qi.z  = dome_damping · edge_mid_z + (1 − dome_damping) · C.z
    # At dome_damping = 2/3 this equals (Pi.z + Pi+1.z + C.z)/3 — the symmetric
    # kite average that sits on Loop's valence-3 fixed point (β = 3/16), so
    # the Q ring keeps its height across subdivision iterations at the
    # default. Moving the slider off 2/3 deliberately walks off the fixed
    # point: low values flatten the dome top, high values sharpen the peak.
    Q_pos = []
    for i in range(6):
        ip1 = (i + 1) % 6
        mid_x = 0.5 * (corner_pos[i][0] + corner_pos[ip1][0])
        mid_y = 0.5 * (corner_pos[i][1] + corner_pos[ip1][1])
        qx = C_pos[0] + dome_area * (mid_x - C_pos[0])
        qy = C_pos[1] + dome_area * (mid_y - C_pos[1])
        edge_mid_z = 0.5 * (corner_pos[i][2] + corner_pos[ip1][2])
        qz = dome_damping * edge_mid_z + (1.0 - dome_damping) * C_pos[2]
        Q_pos.append((qx, qy, qz))

    # Indexing: C = 0, Pi = 1 + i, Qi = 7 + i (i ∈ 0..5).
    C_IDX = 0
    def P_idx(i): return 1 + (i % 6)
    def Q_idx(i): return 7 + (i % 6)

    ctrl_verts = [C_pos] + list(corner_pos) + Q_pos

    # Three triangles per kite (Pi, Pi+1, C) with Qi at the shared interior
    # vertex — winding chosen so every face normal is +Z (CCW viewed from
    # above), matching the rest of the mesh.
    ctrl_faces = []
    for i in range(6):
        ctrl_faces.append((P_idx(i), Q_idx(i), P_idx(i + 1)))
        ctrl_faces.append((P_idx(i), C_IDX,    Q_idx(i)))
        ctrl_faces.append((Q_idx(i), C_IDX,    P_idx(i + 1)))

    ctrl_sharp = [(P_idx(i), P_idx(i + 1)) for i in range(6)]

    sub_verts, sub_faces, rim_chains = subdivide_loop(
        ctrl_verts, ctrl_faces, ctrl_sharp, smoothness_passes
    )
    if resample_density > 0:
        sub_verts, sub_faces, rim_chains = linear_midpoint_subdivide(
            sub_verts, sub_faces, rim_chains, resample_density
        )

    # Number of vertices along a single rim segment after both subdivision
    # stages. Each pass (Loop or linear) halves every segment via midpoint
    # insertion, so segments_per_rim = 2 ** (smoothness + resample).
    rim_density = 2 ** (smoothness_passes + resample_density) + 1

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

    # ---- Register top-surface verts --------------------------------------
    # Rim verts get keyed first under ("corner", i) / ("rim", i, j) so the
    # side-wall n-gon below can look them up. Interior verts get a per-tile
    # counter under ("top", k) — no dedup, just a unique key per vertex.
    top_remap = {}

    for i in range(6):
        chain = rim_chains[i]
        for j, old_idx in enumerate(chain):
            pos = sub_verts[old_idx]
            if j == 0:
                new_idx = add_vert(("corner", i), pos)
            elif j == len(chain) - 1:
                new_idx = add_vert(("corner", (i + 1) % 6), pos)
            else:
                new_idx = add_vert(("rim", i, j), pos)
            top_remap[old_idx] = new_idx

    top_counter = 0
    for old_idx in range(len(sub_verts)):
        if old_idx in top_remap:
            continue
        top_remap[old_idx] = add_vert(("top", top_counter), sub_verts[old_idx])
        top_counter += 1

    # All top-surface verts are now registered at indices 0 .. num_top-1, before
    # any bottom/side/tab geometry. The brush's painted displacement is keyed to
    # exactly this index range; apply it here (z-only, clamped to the base).
    num_top = len(verts_mm)
    have_disp = top_displacement is not None and len(top_displacement) == num_top
    regions = surface_regions or ()
    falloff = max(surface_rim_falloff_mm, 1e-6)
    if have_disp or regions:
        for i in range(num_top):
            x, y, z = verts_mm[i]
            dz = top_displacement[i] if have_disp else 0.0
            if regions:
                fade = rim_edge_distance(x, y, diameter_mm) / falloff
                fade = 0.0 if fade < 0.0 else (1.0 if fade > 1.0 else fade)
                if fade > 0.0:
                    dz += fade * _surface_offset_for_regions(
                        x, y, regions, surface_origin_xy, surface_seed)
            verts_mm[i] = (x, y, max(z + dz, base_thickness_mm))

    for f in sub_faces:
        faces.append(tuple(top_remap[v] for v in f))

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
        if s == rim_density - 1:
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

    # ----- Tab geometry: a vertical extrusion of a rounded-rectangle profile in
    # the per-side (u, depth) plane. The two OUTER (leading) vertical edges are
    # filleted with radius TAB_FILLET_MM so tiles mate more easily; the inner
    # edge (the wall junction) and the two inner corners stay sharp, and most of
    # the outer face stays flat for a solid connection. Per side this is
    # 2 + 4*(SEG+1) new verts and 2 + (2*SEG+3) faces.
    r = TAB_FILLET_MM
    seg = TAB_FILLET_SEGMENTS
    H = TAB_HEIGHT_MM
    D = TAB_DEPTH_MM

    def _arc(cu, cv, start_deg, end_deg):
        # SEG+1 (u, depth) points sweeping start->end about (cu, cv) at radius r.
        pts = []
        for k in range(seg + 1):
            a = math.radians(start_deg + (end_deg - start_deg) * k / seg)
            pts.append((cu + r * math.cos(a), cv + r * math.sin(a)))
        return pts

    for i in range(6):
        outward = rim_normals[i]

        def tab_world(u, depth, z):
            base = point_on_side(i, u, 0.0)
            return (base[0] + depth * outward[0],
                    base[1] + depth * outward[1], z)

        # Free-perimeter profile, walking the lo end cap, the lo fillet, the
        # outer face, the hi fillet and the hi end cap. The closing inner edge
        # (inner_hi -> inner_lo at depth 0) is the wall junction and is left
        # uncapped here — the side-wall n-gon and bottom plate already span it.
        arc_lo = _arc(u_tab_lo + r, D - r, 180.0, 90.0)   # (u_lo,D-r) -> (u_lo+r,D)
        arc_hi = _arc(u_tab_hi - r, D - r, 90.0, 0.0)      # (u_hi-r,D) -> (u_hi,D-r)
        profile = [(u_tab_lo, 0.0)] + arc_lo + arc_hi + [(u_tab_hi, 0.0)]
        last = len(profile) - 1

        ring_bot = []
        ring_top = []
        for j, (u, depth) in enumerate(profile):
            if j == 0:                       # inner_lo (shared with wall/plate)
                bot = vert_index[("bbreak", i, "tab_lo")]
                top = add_vert(("tab_inner_top", i, "lo"), tab_world(u, depth, H))
            elif j == last:                  # inner_hi (shared with wall/plate)
                bot = vert_index[("bbreak", i, "tab_hi")]
                top = add_vert(("tab_inner_top", i, "hi"), tab_world(u, depth, H))
            else:
                bot = add_vert(("tab_ring_bot", i, j), tab_world(u, depth, 0.0))
                top = add_vert(("tab_ring_top", i, j), tab_world(u, depth, H))
            ring_bot.append(bot)
            ring_top.append(top)

        # Caps (wound for outward normals — top +Z, bottom -Z) and one outward
        # wall quad per free profile segment.
        faces.append(tuple(reversed(ring_top)))   # top +Z
        faces.append(tuple(ring_bot))             # bottom -Z
        for j in range(last):
            faces.append((ring_bot[j], ring_bot[j + 1],
                          ring_top[j + 1], ring_top[j]))

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
        for s in range(rim_density):
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
