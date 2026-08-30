"""Local terrain refinement + flattening under planted-tree bases.

No `bpy` imports — same constraint as `mesh_builder.py` / `subdivision.py` /
`procedural_surfaces.py` so this module is unit-testable in plain CPython.

A planted tree keeps a flat base cut but stays perfectly world-vertical
(`flora.py`), so on sloped terrain the base only touches the hex top surface
along one edge. Rather than tilting the tree, `refine_and_flatten` tessellates
a small flat "pad" into the top surface under each tree's footprint and
blends it smoothly back into the surrounding terrain — the same
footprint-flatten-and-blend idea as `operators._compute_snap_gap`, just
against a disc footprint instead of a raycast one.

Called from `mesh_builder.build_hex_tile` on the remapped top-surface
vertices/faces only, strictly after brush/procedural-surface displacement has
already been applied and strictly before any bottom/side/tab geometry is
registered — new vertices this module appends therefore land after the
`0 .. num_top-1` prefix that the brush and snap displacement layers are keyed
to, so planting/unplanting a tree never touches those layers.
"""

import math

try:
    from .mesh_builder import rim_edge_distance, FLORA_NOTCH_MIN_FLOOR_MM
except ImportError:
    from mesh_builder import rim_edge_distance, FLORA_NOTCH_MIN_FLOOR_MM


# Refinement passes are per-edge and stop as soon as no edge qualifies, so
# this is a ceiling on local density near a tree, not a fixed cost.
MAX_LEVELS = 4

# A notch's radius (~1.1mm, see mesh_builder.FLORA_NOTCH_RADIUS_MM) is far
# smaller than a typical pad radius, so cutting one needs much finer local
# edges than a pad's own flatten pass already produces — hence a separate,
# deeper refinement ceiling and a small forced-refinement blend margin.
NOTCH_MAX_LEVELS = 8
NOTCH_REFINE_BLEND_MM = 0.5
# A boundary loop this small can't approximate a circle a real pin will fit
# into — treat it the same as "mesh too coarse to drill" and skip.
NOTCH_MIN_LOOP_VERTS = 8


def _edge_key(a, b):
    return (a, b) if a < b else (b, a)


def _smoothstep(t):
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return t * t * (3.0 - 2.0 * t)


def _dist3(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _point_segment_distance_xy(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    len2 = dx * dx + dy * dy
    if len2 < 1e-18:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / len2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def sample_surface_z(verts, faces, x, y):
    """Interpolated z at (x, y) on a triangulated top surface.

    Barycentric-interpolates z within whichever triangle contains (x, y);
    falls back to the nearest vertex's z if no triangle contains the point
    (guards fp edge cases at the mesh boundary). Only used to pick a pad's
    target height, so an approximate fallback is fine.
    """
    nearest_z = None
    nearest_d2 = None
    for face in faces:
        if len(face) != 3:
            continue
        a, b, c = face
        ax, ay, az = verts[a]
        bx, by, bz = verts[b]
        cx, cy, cz = verts[c]
        denom = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(denom) > 1e-12:
            wa = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / denom
            wb = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / denom
            wc = 1.0 - wa - wb
            if wa >= -1e-9 and wb >= -1e-9 and wc >= -1e-9:
                return wa * az + wb * bz + wc * cz
        for (vx, vy, vz) in ((ax, ay, az), (bx, by, bz), (cx, cy, cz)):
            d2 = (vx - x) ** 2 + (vy - y) ** 2
            if nearest_d2 is None or d2 < nearest_d2:
                nearest_d2 = d2
                nearest_z = vz
    return 0.0 if nearest_z is None else nearest_z


def _edge_qualifies(verts, u, v, pads):
    ux, uy, uz = verts[u]
    vx, vy, vz = verts[v]
    length = _dist3((ux, uy, uz), (vx, vy, vz))
    for p in pads:
        r_pad = p["radius_mm"]
        reach = r_pad + p.get("blend_mm", 0.0)
        if length > r_pad * 0.6 and \
                _point_segment_distance_xy(p["x"], p["y"], ux, uy, vx, vy) <= reach:
            return True
    return False


def _retriangulate(face, marked, mid_idx, verts):
    """Split one triangle per its marked-edge count (0/1/2/3), preserving
    the parent's winding via consecutive-vertex templates (see module notes
    in the implementation plan for the geometric derivation)."""
    t0, t1, t2 = face
    tri = (t0, t1, t2)
    edges = ((t0, t1), (t1, t2), (t2, t0))
    flags = [_edge_key(u, v) in marked for (u, v) in edges]
    n_marked = sum(flags)

    if n_marked == 0:
        return [face]

    if n_marked == 1:
        k = flags.index(True)
        va, vb, vc = tri[k], tri[(k + 1) % 3], tri[(k + 2) % 3]
        m = mid_idx[_edge_key(va, vb)]
        return [(va, m, vc), (m, vb, vc)]

    if n_marked == 2:
        u = flags.index(False)
        ra, rb, rc = tri[(u + 1) % 3], tri[(u + 2) % 3], tri[u]
        m1 = mid_idx[_edge_key(ra, rb)]
        m2 = mid_idx[_edge_key(rb, rc)]
        diag_ra_m2 = _dist3(verts[ra], verts[m2])
        diag_m1_rc = _dist3(verts[m1], verts[rc])
        faces = [(m1, rb, m2)]
        if diag_ra_m2 <= diag_m1_rc:
            faces.append((ra, m1, m2))
            faces.append((ra, m2, rc))
        else:
            faces.append((ra, m1, rc))
            faces.append((m1, m2, rc))
        return faces

    # n_marked == 3 — standard 4-way split.
    m01 = mid_idx[_edge_key(t0, t1)]
    m12 = mid_idx[_edge_key(t1, t2)]
    m20 = mid_idx[_edge_key(t2, t0)]
    return [
        (t0, m01, m20),
        (m01, t1, m12),
        (m20, m12, t2),
        (m01, m12, m20),
    ]


def _iteratively_refine(verts, faces, protected_edges, specs, max_levels):
    """Adaptively subdivide `faces` near any spec in `specs` (pad/notch-shaped
    `{"x","y","radius_mm","blend_mm"}` dicts) until no marked edge remains or
    `max_levels` passes are spent. Mutates `verts` in place (appends only, so
    every existing index stays valid) and returns the retriangulated face
    list. Shared by `refine_and_flatten` (pad radius) and `cut_notches`
    (a much smaller notch radius, needing a deeper ceiling to resolve).
    """
    faces = list(faces)
    for _level in range(max_levels):
        marked = set()
        for face in faces:
            a, b, c = face
            for (u, v) in ((a, b), (b, c), (c, a)):
                key = _edge_key(u, v)
                if key in protected_edges or key in marked:
                    continue
                if _edge_qualifies(verts, u, v, specs):
                    marked.add(key)
        if not marked:
            break

        mid_idx = {}
        for key in marked:
            u, v = key
            ux, uy, uz = verts[u]
            vx, vy, vz = verts[v]
            mid_idx[key] = len(verts)
            verts.append(((ux + vx) / 2.0, (uy + vy) / 2.0, (uz + vz) / 2.0))

        new_faces = []
        for face in faces:
            new_faces.extend(_retriangulate(face, marked, mid_idx, verts))
        faces = new_faces
    return faces


def _boundary_loop(removed_faces):
    """Order the boundary edges of a removed (to-be-drilled) triangle set
    into a single closed, consistently-wound vertex loop.

    Returns `None` if the boundary isn't exactly one simple loop — a pinch
    point (a vertex touched by two disjoint removed "islands", which shows up
    as boundary-edge degree != 2), an open boundary, or several disjoint
    loops — so the caller can skip the cut rather than build a corrupt/
    self-intersecting socket.
    """
    edge_users = {}
    for face in removed_faces:
        a, b, c = face
        for (u, v) in ((a, b), (b, c), (c, a)):
            key = _edge_key(u, v)
            edge_users[key] = edge_users.get(key, 0) + 1

    boundary_out = {}
    degree = {}
    for face in removed_faces:
        a, b, c = face
        for (u, v) in ((a, b), (b, c), (c, a)):
            if edge_users[_edge_key(u, v)] == 1:
                boundary_out.setdefault(u, []).append(v)
                degree[u] = degree.get(u, 0) + 1
                degree[v] = degree.get(v, 0) + 1

    if not boundary_out:
        return None
    if any(d != 2 for d in degree.values()):
        return None
    total_edges = sum(len(v) for v in boundary_out.values())

    start = next(iter(boundary_out))
    loop = [start]
    current = start
    visited_edges = 0
    while True:
        outs = boundary_out.get(current)
        if not outs:
            return None
        nxt = outs.pop()
        visited_edges += 1
        if nxt == start:
            break
        loop.append(nxt)
        current = nxt
        if visited_edges > total_edges:
            return None
    if visited_edges != total_edges:
        return None
    return loop


def cut_notches(verts, faces, protected_edges, notches, warnings=None,
                ok_indices=None, resolved_heights=None):
    """Drill a blind cylindrical socket into `faces` under each notch spec.

    `notches` is a list of `{"x", "y", "radius_mm", "depth_mm"}` dicts
    (tile-local mm) — one per planted tree that should receive a hex-side
    socket for its pin (see `flora.notch_specs`), optionally carrying an
    `"index"` key (the placement index it came from). `verts` is mutated in
    place — appending new wall/floor vertices, and nudging each socket's
    boundary-loop vertices' (x, y) onto an exact circle so a real printed pin
    fits (their z and index are untouched, so this is safe even when a loop
    vertex is an original top-surface vertex the brush/snap displacement
    layers key by index). Returns the replacement top-face list; the caller
    extends its own face list with it, same contract as `refine_and_flatten`.

    A notch that can't be safely cut — the local mesh is too coarse even
    after forced refinement, its boundary is a pinch point/multiple loops,
    it reaches the hex rim, or the tile is too thin for the requested depth
    — is silently skipped (left un-drilled) rather than risking a corrupt or
    non-manifold mesh; a human-readable reason is appended to `warnings` if
    a list is given. Successfully-cut notches append their `"index"` (when
    present) to `ok_indices` if a list is given — the caller (`flora.py`)
    uses this to only create a pin for placements that actually got a real
    socket, so a partial failure can never leave a pin floating with no
    matching cavity. They also record `resolved_heights[index] = pad_z` (the
    exact pre-drill flat pad height) if a dict is given — the caller uses
    this to seat the tree/pin directly at the known height instead of
    raycasting against the mesh this function just put a hole in, which
    would otherwise hit the socket floor instead of the surrounding surface.
    """
    if not notches:
        return list(faces)

    rim_vertex_ids = set()
    for a, b in protected_edges:
        rim_vertex_ids.add(a)
        rim_vertex_ids.add(b)

    refine_specs = [
        {"x": n["x"], "y": n["y"], "radius_mm": n["radius_mm"],
         "blend_mm": NOTCH_REFINE_BLEND_MM}
        for n in notches
    ]
    faces = _iteratively_refine(verts, faces, protected_edges, refine_specs,
                                NOTCH_MAX_LEVELS)

    def _skip(reason, nx, ny, kept, removed):
        if warnings is not None:
            warnings.append(f"flora notch at ({nx:.2f}, {ny:.2f}): {reason}")
        return kept + removed

    for notch in notches:
        nx, ny = notch["x"], notch["y"]
        radius = notch["radius_mm"]
        depth = notch["depth_mm"]

        removed = []
        kept = []
        for face in faces:
            if len(face) == 3 and all(
                math.hypot(verts[v][0] - nx, verts[v][1] - ny) <= radius
                for v in face
            ):
                removed.append(face)
            else:
                kept.append(face)

        if not removed:
            faces = _skip("local mesh too coarse to drill, skipped",
                          nx, ny, kept, removed)
            continue

        loop = _boundary_loop(removed)
        if loop is None or len(loop) < NOTCH_MIN_LOOP_VERTS:
            faces = _skip("irregular local boundary, skipped",
                          nx, ny, kept, removed)
            continue

        if any(v in rim_vertex_ids for v in loop):
            faces = _skip("too close to the hex rim, skipped",
                          nx, ny, kept, removed)
            continue

        pad_z = verts[loop[0]][2]
        if pad_z - depth < FLORA_NOTCH_MIN_FLOOR_MM:
            faces = _skip(
                f"tile too thin for a {depth:.1f}mm-deep socket, skipped",
                nx, ny, kept, removed)
            continue

        # Snap the loop onto an exact circle so a real pin fits the socket —
        # forced refinement already put these vertices close to `radius`.
        for v in loop:
            x, y, z = verts[v]
            d = math.hypot(x - nx, y - ny)
            if d > 1e-9:
                scale = radius / d
                verts[v] = (nx + (x - nx) * scale, ny + (y - ny) * scale, z)
            else:
                verts[v] = (nx + radius, ny, z)

        # `removed` can enclose more than just the loop — a sufficiently
        # refined patch has genuinely interior vertices too. Those are used
        # *only* by removed faces (never by a kept one, else they'd carry a
        # boundary edge and be part of `loop`), so sinking them to the floor
        # in place is safe and avoids ever orphaning them. Loop vertices are
        # still needed at the top (by `kept` and the wall), so they get a
        # fresh bottom counterpart instead of being moved.
        loop_set = set(loop)
        interior_verts = set()
        for f in removed:
            interior_verts.update(f)
        interior_verts -= loop_set

        bottom_z = pad_z - depth
        for v in interior_verts:
            x, y, _z = verts[v]
            verts[v] = (x, y, bottom_z)

        bottom_of = {}
        for v in loop:
            x, y, _z = verts[v]
            bottom_of[v] = len(verts)
            verts.append((x, y, bottom_z))

        n = len(loop)
        wall_faces = []
        for i in range(n):
            top_a, top_b = loop[i], loop[(i + 1) % n]
            bot_a, bot_b = bottom_of[top_a], bottom_of[top_b]
            wall_faces.append((top_a, top_b, bot_b, bot_a))

        # The floor reuses the removed region's own (already valid, already
        # correctly wound) triangulation verbatim, just remapped down: loop
        # corners point at their new bottom counterpart, interior corners
        # keep their own (now-sunk) index.
        floor_faces = [tuple(bottom_of.get(v, v) for v in f) for f in removed]

        faces = kept + wall_faces + floor_faces
        if "index" in notch:
            if ok_indices is not None:
                ok_indices.append(notch["index"])
            if resolved_heights is not None:
                resolved_heights[notch["index"]] = pad_z

    return faces


def refine_and_flatten(verts, faces, protected_edges, pads, diameter_mm, base_thickness_mm):
    """Refine + flatten `faces` (top-surface triangles) under each pad.

    `verts` is mutated in place — new vertices are appended, never inserted,
    so every existing index stays valid. Returns the retriangulated top face
    list; the caller is responsible for extending its own face list with it.

    `pads` is a list of `{"x", "y", "radius_mm", "blend_mm"}` dicts in the
    same local mm frame as `verts`. `protected_edges` is a set of
    `(min_idx, max_idx)` pairs (e.g. the hex rim) that must never be split.
    """
    if not pads:
        return list(faces)

    # Sample every pad's target height up front, from the pre-flatten
    # surface, so two nearby pads can't influence each other's target.
    pad_z = [sample_surface_z(verts, faces, p["x"], p["y"]) for p in pads]

    faces = _iteratively_refine(verts, faces, protected_edges, pads, MAX_LEVELS)

    for i in range(len(verts)):
        x, y, z = verts[i]
        for p, pz in zip(pads, pad_z):
            r_pad = p["radius_mm"]
            r_blend = p.get("blend_mm", 0.0)
            d = math.hypot(x - p["x"], y - p["y"])
            if d <= r_pad:
                w = 1.0
            elif r_blend > 1e-9 and d <= r_pad + r_blend:
                w = 1.0 - _smoothstep((d - r_pad) / r_blend)
            else:
                w = 0.0
            if w <= 0.0:
                continue
            if r_blend > 1e-9:
                # Fade the pad out near the hex rim so it can never desync a
                # seam with the neighbouring tile (mirrors the snap-to-model
                # skirt fade in operators._compute_snap_gap).
                rim = rim_edge_distance(x, y, diameter_mm)
                w *= 0.0 if rim < 0.0 else (1.0 if rim > r_blend else rim / r_blend)
            if w <= 0.0:
                continue
            z = z + w * (pz - z)
        verts[i] = (x, y, max(z, base_thickness_mm))

    return faces
