"""Loop subdivision over an open triangle mesh with sharp-edge support.

No `bpy` imports — same constraint as `map.py` / `mesh_builder.py` so the
unit tests run under plain CPython.

Triangles only. Vertices are (x, y, z) tuples. Sharp edges keep their
straight-line geometry (linear midpoint, no smoothing). A vertex with two
or more incident sharp edges is held fixed; this matches the "corner"
crease rule in the Pixar/Hoppe extended Loop scheme. In our use case
every sharp-edge vertex falls into that bucket, so the crease (1/8, 3/4,
1/8) stencil never actually fires — but it is implemented for the single
edge endpoint case to make the module self-contained.
"""

import math


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _beta(n):
    """Loop's interior vertex weight for valence n (Warren-style closed form).

    β = (1/n)·(5/8 − (3/8 + (1/4)·cos(2π/n))²); 3/16 at n=3 (the special case
    falls out of the formula but using it directly avoids a tiny FP wobble).
    """
    if n == 3:
        return 3.0 / 16.0
    c = math.cos(2.0 * math.pi / n)
    inner = 3.0 / 8.0 + 0.25 * c
    return (1.0 / n) * (5.0 / 8.0 - inner * inner)


def _edge_key(a, b):
    return (a, b) if a < b else (b, a)


def _subdivide_once(verts, faces, sharp_edges, rim_chains):
    """One Loop pass. Returns (verts_new, faces_new, sharp_new, rim_chains_new)."""
    # Build edge → list of (face_index, opposite_vertex) map. Used both to
    # find the two interior neighbours for the (3/8, 3/8, 1/8, 1/8) midpoint
    # stencil and to assign unique midpoint vertex indices.
    edges = {}
    for face in faces:
        a, b, c = face
        for x, y, op in ((a, b, c), (b, c, a), (c, a, b)):
            edges.setdefault(_edge_key(x, y), []).append(op)

    sharp_set = set(_edge_key(a, b) for (a, b) in sharp_edges)

    # Per-vertex one-ring + incident sharp edge endpoints.
    n_v = len(verts)
    nbr = [set() for _ in range(n_v)]
    sharp_nbr = [[] for _ in range(n_v)]
    for key in edges:
        a, b = key
        nbr[a].add(b)
        nbr[b].add(a)
        if key in sharp_set:
            sharp_nbr[a].append(b)
            sharp_nbr[b].append(a)

    # ---- Update existing vertex positions ---------------------------------
    new_verts = [None] * n_v
    for i in range(n_v):
        sharp_count = len(sharp_nbr[i])
        if sharp_count >= 2:
            # Corner (incl. straight-rim "crease vertex" — same result here
            # because the two sharp neighbours are colinear with the vertex
            # by construction).
            new_verts[i] = verts[i]
        elif sharp_count == 1:
            # "Dart" vertex — endpoint of exactly one sharp edge. Does not
            # occur in HexFinity's closed rim loop; held fixed here for a
            # safe default rather than guessing a stencil.
            new_verts[i] = verts[i]
        else:
            n = len(nbr[i])
            if n == 0:
                new_verts[i] = verts[i]
                continue
            b = _beta(n)
            pos = _scale(verts[i], 1.0 - n * b)
            for j in nbr[i]:
                pos = _add(pos, _scale(verts[j], b))
            new_verts[i] = pos

    # ---- Insert edge-midpoint vertices ------------------------------------
    edge_mid_idx = {}
    for key, opps in edges.items():
        a, b = key
        if key in sharp_set:
            mid = _scale(_add(verts[a], verts[b]), 0.5)
        else:
            # (3/8)A + (3/8)B + (1/8)L + (1/8)R — interior Loop midpoint.
            assert len(opps) == 2, (
                f"interior edge {key} has {len(opps)} faces (expected 2)"
            )
            L = verts[opps[0]]
            R = verts[opps[1]]
            mid = _add(
                _add(_scale(verts[a], 3.0 / 8.0), _scale(verts[b], 3.0 / 8.0)),
                _add(_scale(L, 1.0 / 8.0), _scale(R, 1.0 / 8.0)),
            )
        edge_mid_idx[key] = len(new_verts)
        new_verts.append(mid)

    # ---- Emit 4 sub-triangles per parent ----------------------------------
    new_faces = []
    for face in faces:
        a, b, c = face
        m_ab = edge_mid_idx[_edge_key(a, b)]
        m_bc = edge_mid_idx[_edge_key(b, c)]
        m_ca = edge_mid_idx[_edge_key(c, a)]
        new_faces.append((a, m_ab, m_ca))
        new_faces.append((m_ab, b, m_bc))
        new_faces.append((m_ca, m_bc, c))
        new_faces.append((m_ab, m_bc, m_ca))

    # The two sub-edges of an originally-sharp edge stay sharp; everything
    # else (including the centre triangle's three edges) becomes smooth.
    new_sharp = []
    for (a, b) in sharp_edges:
        m = edge_mid_idx[_edge_key(a, b)]
        new_sharp.append((a, m))
        new_sharp.append((m, b))

    # Grow each rim chain by inserting the midpoint between every consecutive
    # pair. Preserves order so the caller can walk it as the densified rim.
    new_rim_chains = []
    for chain in rim_chains:
        new_chain = [chain[0]]
        for k in range(len(chain) - 1):
            u, v = chain[k], chain[k + 1]
            new_chain.append(edge_mid_idx[_edge_key(u, v)])
            new_chain.append(v)
        new_rim_chains.append(new_chain)

    return new_verts, new_faces, new_sharp, new_rim_chains


def subdivide_loop(verts, faces, sharp_edges, levels):
    """Run `levels` passes of Loop subdivision with crease support.

    Args:
        verts: list of (x, y, z) input vertices.
        faces: list of (a, b, c) triangle vertex indices.
        sharp_edges: iterable of (a, b) endpoint pairs whose edges are
            tagged sharp (boundary or crease). Endpoints with 2+ incident
            sharp edges are held fixed across passes.
        levels: number of subdivision passes.

    Returns:
        (verts, faces, rim_chains). `rim_chains[i]` is the ordered list of
        vertex indices that descended from `sharp_edges[i]` in the output
        mesh, starting at the original `a` endpoint and ending at `b`.
    """
    verts = [tuple(v) for v in verts]
    faces = [tuple(f) for f in faces]
    sharp_edges = [tuple(e) for e in sharp_edges]
    rim_chains = [[a, b] for (a, b) in sharp_edges]
    for _ in range(levels):
        verts, faces, sharp_edges, rim_chains = _subdivide_once(
            verts, faces, sharp_edges, rim_chains
        )
    return verts, faces, rim_chains


def linear_midpoint_subdivide(verts, faces, rim_chains, levels):
    """Linearly midpoint-subdivide each triangle into 4 — no smoothing.

    Each new vertex is the exact chord midpoint of its parent edge. Used to
    densify a mesh for downstream displacement/texturing without altering
    its shape (a denser triangulation of the same piecewise-flat surface).
    """
    verts = [tuple(v) for v in verts]
    faces = [tuple(f) for f in faces]
    rim_chains = [list(c) for c in rim_chains]
    for _ in range(levels):
        edge_mid_idx = {}
        # Two-pass: build the unique edge set first so indices are stable
        # regardless of which face introduced the edge.
        for face in faces:
            a, b, c = face
            for x, y in ((a, b), (b, c), (c, a)):
                edge_mid_idx.setdefault(_edge_key(x, y), None)
        for key in edge_mid_idx:
            a, b = key
            edge_mid_idx[key] = len(verts)
            verts.append(_scale(_add(verts[a], verts[b]), 0.5))

        new_faces = []
        for face in faces:
            a, b, c = face
            m_ab = edge_mid_idx[_edge_key(a, b)]
            m_bc = edge_mid_idx[_edge_key(b, c)]
            m_ca = edge_mid_idx[_edge_key(c, a)]
            new_faces.append((a, m_ab, m_ca))
            new_faces.append((m_ab, b, m_bc))
            new_faces.append((m_ca, m_bc, c))
            new_faces.append((m_ab, m_bc, m_ca))
        faces = new_faces

        new_rim_chains = []
        for chain in rim_chains:
            new_chain = [chain[0]]
            for k in range(len(chain) - 1):
                u, v = chain[k], chain[k + 1]
                new_chain.append(edge_mid_idx[_edge_key(u, v)])
                new_chain.append(v)
            new_rim_chains.append(new_chain)
        rim_chains = new_rim_chains

    return verts, faces, rim_chains
