"""Pure-Python face-normal flood fill for the Draw Area "Flood Fill" tool.

No `bpy` imports — unit-testable in plain CPython. Turns a single clicked
top-surface face into a connected patch of similarly-oriented faces, then
into a tile-local-XY boundary polygon that plugs straight into the existing
`HexFinitySurfaceRegion` authoring path (`regions.py`'s `_commit_region`).

Flood fill is only ever an *authoring* input method here, never a stored
representation: a mesh face's index does not survive a subdivision/pad/
path/notch rebuild the way a region's continuous-XY polygon does (see
`procedural_surfaces.region_mask`'s docstring), so the result of a fill is
converted to a polygon immediately and the face indices are discarded.
"""

import math


def _edge_key(a, b):
    return (a, b) if a < b else (b, a)


def face_normal(verts, face):
    """Unit normal of `face` (>=3 verts) via Newell's method (summed over
    every edge, not just the first three vertices). A first-three-vertex
    cross product is only reliable for a genuinely planar/convex face; the
    tile mesh also has large concave side-wall/tab n-gons whose first three
    vertices can be a near-collinear run along the top rim before the face
    dives down a near-vertical wall — a plain cross product there reads as
    "flat and upward" even though the face as a whole is a wall. Newell's
    method averages over the whole boundary, so it's immune to that.
    Returns (0.0, 0.0, 0.0) for a degenerate (zero-area) face."""
    nx = ny = nz = 0.0
    n = len(face)
    for k in range(n):
        x1, y1, z1 = verts[face[k]]
        x2, y2, z2 = verts[face[(k + 1) % n]]
        nx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    m = math.sqrt(nx * nx + ny * ny + nz * nz)
    if m <= 1e-18:
        return (0.0, 0.0, 0.0)
    return (nx / m, ny / m, nz / m)


def build_face_adjacency(faces):
    """{edge_key: [face_index, ...]} over every edge of every face — the
    same edge-keying idiom as `manifold_check.assert_two_manifold` /
    `subdivision._edge_key`, but keeping the incident face indices instead
    of just a count."""
    adjacency = {}
    for i, face in enumerate(faces):
        n = len(face)
        for k in range(n):
            a, b = face[k], face[(k + 1) % n]
            adjacency.setdefault(_edge_key(a, b), []).append(i)
    return adjacency


def flood_fill_faces(verts, faces, seed_index, angle_threshold_deg, up_epsilon=0.05):
    """Connected faces within `angle_threshold_deg` of the seed face's normal.

    Grows from `seed_index` across shared edges; a candidate face is
    included — and expanded through — only if it faces upward past
    `up_epsilon` (a hard safety net that keeps the fill off near-vertical
    side walls and the downward-facing bottom plate, independent of how
    large a threshold the caller picks) AND its normal is within the
    threshold of the *seed's* normal. Comparing every candidate to the fixed
    seed normal (rather than to its immediate neighbour) matches standard
    magic-wand semantics — "tolerance from the clicked point" — and keeps
    the selection from creeping indefinitely across a gently curved dome.

    Returns an empty set if `seed_index` is out of range or the seed face
    itself doesn't face upward.
    """
    if not (0 <= seed_index < len(faces)):
        return set()
    seed_normal = face_normal(verts, faces[seed_index])
    if seed_normal[2] <= up_epsilon:
        return set()

    cos_threshold = math.cos(math.radians(max(0.0, angle_threshold_deg)))
    adjacency = build_face_adjacency(faces)

    def accepts(i):
        n = face_normal(verts, faces[i])
        if n[2] <= up_epsilon:
            return False
        dot = (n[0] * seed_normal[0] + n[1] * seed_normal[1]
               + n[2] * seed_normal[2])
        return dot >= cos_threshold

    selected = {seed_index}
    frontier = [seed_index]
    while frontier:
        i = frontier.pop()
        face = faces[i]
        n = len(face)
        for k in range(n):
            a, b = face[k], face[(k + 1) % n]
            for j in adjacency.get(_edge_key(a, b), ()):
                if j in selected or j == i:
                    continue
                if accepts(j):
                    selected.add(j)
                    frontier.append(j)
    return selected


def boundary_loop(faces, selected):
    """Order the boundary edges of `selected` (a set of indices into
    `faces`, each a triangle) into a single closed vertex-index loop.

    Returns `None` if the boundary isn't exactly one simple loop — a pinch
    point (boundary-edge degree != 2 at some vertex), an open boundary, or
    several disjoint loops (the selection wraps a hole or splits into
    islands) — mirroring `tree_pads._boundary_loop`'s "skip rather than
    risk a bad result" rule, so the caller can report a warning instead of
    guessing which loop is the outer one. Faces may be any arity (the top
    surface is always triangles in practice, but this doesn't assume it).
    """
    def _edges(face):
        n = len(face)
        return [(face[k], face[(k + 1) % n]) for k in range(n)]

    selected_faces = [faces[i] for i in selected]
    edge_users = {}
    for face in selected_faces:
        for (u, v) in _edges(face):
            key = _edge_key(u, v)
            edge_users[key] = edge_users.get(key, 0) + 1

    boundary_out = {}
    degree = {}
    for face in selected_faces:
        for (u, v) in _edges(face):
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


def loop_to_xy(verts, loop):
    """Tile-local (x, y) mm for each vertex index in a boundary loop."""
    return [(verts[i][0], verts[i][1]) for i in loop]
