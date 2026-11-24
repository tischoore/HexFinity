"""Tests for the Flagstone Path's flattened bed
(`tree_pads.refine_and_flatten_bed_along_path`, the `"bed"` path kind)."""

import math

import pytest

import tree_pads
from manifold_check import assert_two_manifold
from mesh_builder import build_hex_tile, top_vertex_count


# Local copies of test_tree_pads.py's open-patch helpers.
def _edge_key(a, b):
    return (a, b) if a < b else (b, a)


def _grid_mesh(n, size):
    """n x n grid of quads (2 tris each) over [0,size] x [0,size] at z=0, plus
    its outer boundary loop as a protected-edge set — a simple open patch for
    exercising refine_and_flatten's retriangulation directly, independent of
    the hex builder's closed-mesh geometry."""
    step = size / n
    index = {}
    verts = []
    for j in range(n + 1):
        for i in range(n + 1):
            index[(i, j)] = len(verts)
            verts.append((i * step, j * step, 0.0))
    faces = []
    for j in range(n):
        for i in range(n):
            a, b = index[(i, j)], index[(i + 1, j)]
            c, d = index[(i + 1, j + 1)], index[(i, j + 1)]
            faces.append((a, b, c))
            faces.append((a, c, d))
    protected = set()
    for i in range(n):
        protected.add(_edge_key(index[(i, 0)], index[(i + 1, 0)]))
        protected.add(_edge_key(index[(i, n)], index[(i + 1, n)]))
    for j in range(n):
        protected.add(_edge_key(index[(0, j)], index[(0, j + 1)]))
        protected.add(_edge_key(index[(n, j)], index[(n, j + 1)]))
    return verts, faces, protected


def _assert_crack_free(faces, protected):
    """Custom manifold check for an OPEN patch (assert_two_manifold requires
    a closed mesh, which a grid patch isn't): every interior edge must be
    shared by exactly 2 faces (no T-junctions); every protected boundary edge
    stays open (shared by exactly 1), proving it was never split."""
    edge_count = {}
    for face in faces:
        n = len(face)
        for k in range(n):
            key = _edge_key(face[k], face[(k + 1) % n])
            edge_count[key] = edge_count.get(key, 0) + 1
    for key, c in edge_count.items():
        if key in protected:
            assert c == 1, f"protected boundary edge {key} was split (count {c})"
        else:
            assert c == 2, f"non-manifold interior edge {key} (count {c})"




def _bumpy(x, y):
    # Cross-slope + bumps, so "flat across the corridor" is non-trivial.
    return 12.0 + 0.05 * y + 0.4 * math.sin(x * 0.7) * math.cos(y * 0.9)


def _bumpy_grid(n=20, size=60.0):
    verts, faces, protected = _grid_mesh(n, size)
    verts = [(x, y, _bumpy(x, y)) for (x, y, _z) in verts]
    return verts, faces, protected


BED = {"points": [(5.0, 30.0), (55.0, 30.0)], "width_mm": 10.0,
       "blend_mm": 3.0, "local_subdiv": 2}


def test_bed_is_flat_across_inside_corridor():
    verts, faces, protected = _bumpy_grid()
    before = list(verts)
    tree_pads.refine_and_flatten_bed_along_path(
        verts, faces, protected, [BED], diameter_mm=1e6, base_thickness_mm=0.0)
    # Group every vertex well inside the corridor by its x (arc-length along
    # this straight path): the bed is flat ACROSS, so all z at one x agree.
    by_x = {}
    for (x, y, z) in verts:
        if 10.0 <= x <= 50.0 and abs(y - 30.0) <= 4.0:
            by_x.setdefault(round(x, 6), []).append(z)
    assert by_x
    for zs in by_x.values():
        assert max(zs) - min(zs) < 1e-6
    # ...and it really changed the bumpy terrain (cross-slope removed).
    assert any(abs(a[2] - b[2]) > 0.05 for a, b in zip(before, verts))


def test_bed_leaves_far_vertices_untouched_and_crack_free():
    verts, faces, protected = _bumpy_grid()
    orig = list(verts)
    faces = tree_pads.refine_and_flatten_bed_along_path(
        verts, faces, protected, [BED], diameter_mm=1e6, base_thickness_mm=0.0)
    _assert_crack_free(faces, protected)
    for i, (x, y, z) in enumerate(orig):
        if abs(y - 30.0) > 5.0 + 3.0 + 1e-6:
            assert verts[i][2] == z


def test_no_beds_is_noop():
    verts, faces, protected = _bumpy_grid(4, 10.0)
    before = list(verts)
    out = tree_pads.refine_and_flatten_bed_along_path(
        verts, faces, protected, [], diameter_mm=1e6, base_thickness_mm=0.0)
    assert verts == before and out == list(faces)


def _tile(**kw):
    base = dict(diameter_mm=100.0, level_height_mm=4.0, base_thickness_mm=10.0,
                corner_levels=(0, 1, 2, 3, 2, 1), center_level=None,
                smoothness_passes=3, resample_density=0)
    base.update(kw)
    return build_hex_tile(**base)


def test_build_with_bed_is_manifold_and_keeps_top_prefix():
    bed = {"kind": "bed", "points": [(-48.0, 0.0), (48.0, 0.0)],
           "width_mm": 12.0, "blend_mm": 2.0, "local_subdiv": 2}
    plain_v, _ = _tile()
    verts, faces = _tile(path_features=[bed])
    assert_two_manifold(verts, faces)
    n_top = top_vertex_count(3, 0)
    # Top-prefix count is unaffected (append-only); rim/seam vertices are
    # protected by the rim fade.
    assert len(verts) > len(plain_v)
    changed = sum(1 for a, b in zip(plain_v[:n_top], verts[:n_top])
                  if abs(a[2] - b[2]) > 1e-9)
    assert changed > 0
