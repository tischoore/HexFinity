import math
import pytest

from mesh_builder import (
    build_hex_tile,
    top_vertex_count,
    TAB_FILLET_SEGMENTS,
)
from manifold_check import assert_two_manifold
import tree_pads


# ---------------------------------------------------------------------------
# Bottom-region face count — same closed form as test_mesh_builder.py's
# _BOTTOM_FACES. Bottom/wall/tab/hole geometry never depends on flora_pads,
# so this constant lets tests slice "the last N faces" out of a build without
# having to know how many top faces refinement produced.
_TAB_FACES = 6 * (2 + (2 * TAB_FILLET_SEGMENTS + 3))
_BOTTOM_FACES = 6 + _TAB_FACES + 24 + 42


def _tile(**kw):
    base = dict(
        diameter_mm=220.0, level_height_mm=10.0, base_thickness_mm=10.0,
        corner_levels=(0, 0, 0, 0, 0, 0), center_level=None,
        smoothness_passes=3, resample_density=0,
    )
    base.update(kw)
    return build_hex_tile(**base)


def _sloped_tile(**kw):
    # A real slope so a flat pad has something to flatten against.
    base = dict(corner_levels=(0, 1, 2, 3, 4, 5), center_level=None)
    base.update(kw)
    return _tile(**base)


CENTER_PAD = [{"x": 0.0, "y": 0.0, "radius_mm": 8.0, "blend_mm": 5.0}]


# ---------------------------------------------------------------------------
# 1. Manifold / crack-free.

def test_padded_tile_is_manifold():
    verts, faces = _sloped_tile(flora_pads=CENTER_PAD)
    assert_two_manifold(verts, faces)


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


def test_refine_and_flatten_grid_is_crack_free_and_protects_boundary():
    verts, faces, protected = _grid_mesh(6, 60.0)
    pads = [{"x": 30.0, "y": 30.0, "radius_mm": 5.0, "blend_mm": 3.0}]
    new_faces = tree_pads.refine_and_flatten(
        verts, faces, protected, pads, diameter_mm=1000.0, base_thickness_mm=-1e9)
    _assert_crack_free(new_faces, protected)
    referenced = set()
    for f in new_faces:
        referenced.update(f)
    assert referenced == set(range(len(verts))), "orphan vertex after refinement"


# ---------------------------------------------------------------------------
# 2. Rim / bottom / wall / tab geometry byte-identical with and without pads.

def test_pad_leaves_bottom_wall_tab_geometry_untouched():
    num_top = top_vertex_count(3, 0)
    verts_plain, faces_plain = _sloped_tile()
    verts_padded, faces_padded = _sloped_tile(flora_pads=CENTER_PAD)

    extra = len(verts_padded) - len(verts_plain)
    assert extra > 0, "pad should have added at least one refinement vertex"

    bottom_plain = faces_plain[-_BOTTOM_FACES:]
    bottom_padded = faces_padded[-_BOTTOM_FACES:]

    def shift(face):
        return tuple(v + extra if v >= num_top else v for v in face)

    assert [shift(f) for f in bottom_plain] == bottom_padded


# ---------------------------------------------------------------------------
# 3. Layer prefix stable — top_vertex_count()/brush/snap layers unaffected.

def test_top_prefix_unchanged_by_padding():
    # Padding is z-only (a lerp toward pad height) — every original top vert's
    # XY, and the sheer count/order of the prefix, must survive untouched even
    # when its z is legitimately flattened.
    num_top = top_vertex_count(3, 0)
    verts_plain, _ = _sloped_tile()
    verts_padded, _ = _sloped_tile(flora_pads=CENTER_PAD)
    assert len(verts_plain) >= num_top
    assert len(verts_padded) >= num_top
    for i in range(num_top):
        assert verts_padded[i][:2] == pytest.approx(verts_plain[i][:2], abs=1e-9)


# ---------------------------------------------------------------------------
# 4. Pad interior is flat.

def test_pad_interior_is_flat():
    base_thickness = 10.0
    pad = {"x": 0.0, "y": 0.0, "radius_mm": 10.0, "blend_mm": 6.0}
    verts, _ = _sloped_tile(flora_pads=[pad])
    # Restrict to top-surface verts: bottom/tab/hole geometry can sit at the
    # same (0, 0) XY (e.g. the bottom-plate fan centre) but every top vert is
    # clamped to >= base_thickness_mm, while bottom/tab/hole z tops out at
    # TAB_HEIGHT_MM + tolerance (< base_thickness_mm here), so this threshold
    # cleanly separates the two without needing to know exact indices.
    inside = [v for v in verts
              if math.hypot(v[0] - pad["x"], v[1] - pad["y"]) <= pad["radius_mm"] - 1e-6
              and v[2] >= base_thickness - 1e-6]
    assert len(inside) >= 3, "expected several verts inside the pad after refinement"
    z0 = inside[0][2]
    for v in inside:
        assert v[2] == pytest.approx(z0, abs=1e-6)


# ---------------------------------------------------------------------------
# 5. Clean blend beyond r_pad + r_blend — bit-identical to the unpadded build.

def test_verts_beyond_blend_reach_are_unchanged():
    pad = {"x": 0.0, "y": 0.0, "radius_mm": 6.0, "blend_mm": 4.0}
    reach = pad["radius_mm"] + pad["blend_mm"]
    num_top = top_vertex_count(3, 0)
    verts_plain, _ = _sloped_tile()
    verts_padded, _ = _sloped_tile(flora_pads=[pad])
    for i in range(num_top):
        x, y, z = verts_plain[i]
        if math.hypot(x - pad["x"], y - pad["y"]) > reach + 1e-6:
            assert verts_padded[i] == pytest.approx((x, y, z), abs=1e-9)


# ---------------------------------------------------------------------------
# 6. Local + bounded growth.

def test_no_pads_adds_zero_vertices():
    verts_plain, _ = _sloped_tile()
    verts_none, _ = _sloped_tile(flora_pads=None)
    verts_empty, _ = _sloped_tile(flora_pads=[])
    assert len(verts_none) == len(verts_plain)
    assert len(verts_empty) == len(verts_plain)


def test_one_pad_growth_is_small_and_local():
    verts_plain, _ = _sloped_tile()
    verts_padded, _ = _sloped_tile(flora_pads=CENTER_PAD)
    extra = len(verts_padded) - len(verts_plain)
    # A single small pad should refine a local neighbourhood, not the tile —
    # nowhere near a full extra Loop/resample pass over the whole mesh.
    assert 0 < extra < 300


# ---------------------------------------------------------------------------
# 7. Determinism.

def test_padded_build_is_deterministic():
    a = _sloped_tile(flora_pads=CENTER_PAD)
    b = _sloped_tile(flora_pads=CENTER_PAD)
    assert a[0] == pytest.approx(list(b[0]), abs=1e-12)
    assert a[1] == b[1]


# ---------------------------------------------------------------------------
# 8. Rim fade — a pad near a hex edge leaves the rim itself untouched.

def test_pad_near_rim_leaves_rim_corners_at_analytic_height():
    R = 220.0 / 2.0
    diameter = 220.0
    lh = 10.0
    base = 10.0
    levels = (0, 1, 2, 3, 4, 5)
    # A pad centred just inside the P2 corner (+X rim), close enough that its
    # blend band would otherwise reach the rim.
    pad = {"x": R - 6.0, "y": 0.0, "radius_mm": 6.0, "blend_mm": 5.0}
    verts, faces = build_hex_tile(
        diameter_mm=diameter, level_height_mm=lh, base_thickness_mm=base,
        corner_levels=levels, center_level=None, smoothness_passes=3,
        flora_pads=[pad],
    )
    assert_two_manifold(verts, faces)
    corner_z = [base + L * lh for L in levels]
    for i in range(6):
        angle = math.pi / 3.0 - i * (math.pi / 3.0)
        cx, cy = R * math.cos(angle), R * math.sin(angle)
        match = [v for v in verts
                 if abs(v[0] - cx) < 1e-6 and abs(v[1] - cy) < 1e-6]
        assert match, f"corner {i} vertex missing"
        assert match[0][2] == pytest.approx(corner_z[i], abs=1e-9)


# ---------------------------------------------------------------------------
# 9. Pad interior beats procedural-surface texture (lerp, not additive).

CENTER_REGION = [{
    "surface_type": "COBBLESTONE", "feature_mm": 12.0, "depth_mm": 4.0,
    "regularity": 0.4, "direction_rad": 0.0, "polygon": [],
    "mask_falloff_mm": 0.0,
}]


def test_pad_interior_flat_despite_active_procedural_texture():
    pad = {"x": 0.0, "y": 0.0, "radius_mm": 10.0, "blend_mm": 6.0}
    flat, _ = _tile(surface_regions=CENTER_REGION)
    padded, _ = _tile(surface_regions=CENTER_REGION, flora_pads=[pad])

    # Texture is active somewhere on the flat build (sanity: the fixture
    # actually displaces the surface away from a perfectly flat tile).
    assert any(abs(v[2] - 10.0) > 1e-6 for v in flat)

    base_thickness = 10.0
    inside = [v for v in padded
              if math.hypot(v[0] - pad["x"], v[1] - pad["y"]) <= pad["radius_mm"] - 1e-6
              and v[2] >= base_thickness - 1e-6]
    assert len(inside) >= 3
    z0 = inside[0][2]
    for v in inside:
        assert v[2] == pytest.approx(z0, abs=1e-6)


# ---------------------------------------------------------------------------
# 10. sample_surface_z on known planar geometry.

def test_sample_surface_z_planar_triangle():
    verts = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 5.0)]
    faces = [(0, 1, 2)]
    # Centroid: barycentric-average of the three z values.
    z = tree_pads.sample_surface_z(verts, faces, 10.0 / 3.0, 10.0 / 3.0)
    assert z == pytest.approx(5.0 / 3.0, abs=1e-9)
    # A known point exactly at a vertex.
    assert tree_pads.sample_surface_z(verts, faces, 0.0, 10.0) == pytest.approx(5.0, abs=1e-9)


def test_sample_surface_z_outside_mesh_falls_back_to_nearest_vertex():
    verts = [(0.0, 0.0, 1.0), (10.0, 0.0, 2.0), (0.0, 10.0, 3.0)]
    faces = [(0, 1, 2)]
    z = tree_pads.sample_surface_z(verts, faces, -100.0, -100.0)
    assert z == pytest.approx(1.0, abs=1e-9)   # nearest vertex is (0,0)
