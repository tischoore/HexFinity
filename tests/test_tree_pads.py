import math
import pytest

from mesh_builder import (
    build_hex_tile,
    top_vertex_count,
    TAB_FILLET_SEGMENTS,
    FLORA_PIN_RADIUS_MM,
    FLORA_NOTCH_RADIUS_MM,
    FLORA_NOTCH_DEPTH_MM,
    FLORA_PIN_LENGTH_MM,
    FLORA_NOTCH_MIN_FLOOR_MM,
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


# ---------------------------------------------------------------------------
# 11. cut_notches — pin/socket interlock.

CENTER_NOTCH = [{"x": 0.0, "y": 0.0, "radius_mm": FLORA_NOTCH_RADIUS_MM,
                 "depth_mm": FLORA_NOTCH_DEPTH_MM}]


def _check_consistent_winding(faces, protected=frozenset()):
    """Every INTERIOR edge (i.e. not an open-boundary `protected` edge) must
    be traversed in OPPOSITE directions by the two faces sharing it, so both
    faces' outward normals agree with the whole-mesh CCW-from-above
    convention. This is a real gap in `assert_two_manifold` (it only checks
    that the undirected edge is used twice, not that the two uses disagree in
    direction) and is exactly where a new cavity type could sneak in an
    inside-out face. A `protected` open-boundary edge legitimately has only
    one directed use (there's no face on its other side)."""
    directed_owner = {}
    for face in faces:
        n = len(face)
        for k in range(n):
            a, b = face[k], face[(k + 1) % n]
            assert (a, b) not in directed_owner, (
                f"edge {(a, b)} traversed the same direction by two faces "
                f"(inconsistent/inverted winding)")
            directed_owner[(a, b)] = face
    for (a, b) in directed_owner:
        if _edge_key(a, b) in protected:
            continue
        assert (b, a) in directed_owner, f"edge {(a, b)} has no opposite pair"


def test_notched_tile_is_manifold():
    pad = {"x": 0.0, "y": 0.0, "radius_mm": 8.0, "blend_mm": 5.0}
    verts, faces = _sloped_tile(flora_pads=[pad], flora_notches=CENTER_NOTCH)
    assert_two_manifold(verts, faces)
    # Winding consistency is checked on the synthetic grid fixture below —
    # the hex builder's own tab/hole geometry has a pre-existing winding
    # defect unrelated to flora (see the code-review note), which would make
    # a whole-tile winding check here fail for reasons outside this feature.


def test_notch_requires_a_pad_to_land_on_a_flat_disc():
    # Without a pad the ground under the notch is sloped; cutting still must
    # not corrupt the mesh even though the socket mouth then isn't perfectly
    # flat in practice (pin/notch generation is gated on flatten_base by the
    # caller — this only proves cut_notches itself stays safe either way).
    verts, faces = _sloped_tile(flora_notches=CENTER_NOTCH)
    assert_two_manifold(verts, faces)


def _elevated_grid_mesh(n, size, z):
    """`_grid_mesh` sits at z=0, which is too shallow for any real notch depth
    given `FLORA_NOTCH_MIN_FLOOR_MM`'s absolute-z floor-depth guard. Lift the
    whole patch to `z` so these synthetic tests can exercise a real cut."""
    verts, faces, protected = _grid_mesh(n, size)
    verts = [(x, y, z) for (x, y, _z) in verts]
    return verts, faces, protected


def test_cut_notches_grid_is_crack_free():
    top_z = 20.0
    verts, faces, protected = _elevated_grid_mesh(6, 60.0, top_z)
    notch = {"x": 30.0, "y": 30.0, "radius_mm": FLORA_NOTCH_RADIUS_MM,
             "depth_mm": 5.0}
    warnings = []
    new_faces = tree_pads.cut_notches(verts, faces, protected, [notch],
                                      warnings=warnings)
    assert warnings == [], f"unexpected skip: {warnings}"
    _assert_crack_free(new_faces, protected)
    _check_consistent_winding(new_faces, protected)


def test_cut_notches_reports_resolved_height():
    # The caller (flora.py) uses this instead of raycasting against a mesh
    # that now has a hole exactly where it would aim — must be the exact
    # pre-drill flat height, not an approximation.
    top_z = 20.0
    verts, faces, protected = _elevated_grid_mesh(6, 60.0, top_z)
    notch = {"x": 30.0, "y": 30.0, "radius_mm": FLORA_NOTCH_RADIUS_MM,
             "depth_mm": 5.0, "index": 3}
    ok_indices = []
    resolved_heights = {}
    tree_pads.cut_notches(verts, faces, protected, [notch],
                          ok_indices=ok_indices, resolved_heights=resolved_heights)
    assert ok_indices == [3]
    assert resolved_heights == {3: pytest.approx(top_z)}


def test_cut_notches_skip_reports_no_resolved_height():
    monkeypatch_notch = {"x": 30.0, "y": 30.0, "radius_mm": FLORA_NOTCH_RADIUS_MM,
                         "depth_mm": 5.0, "index": 7}
    verts, faces, protected = _grid_mesh(6, 60.0)   # z=0 -> too thin, skipped
    resolved_heights = {}
    ok_indices = []
    tree_pads.cut_notches(verts, faces, protected, [monkeypatch_notch],
                          ok_indices=ok_indices, resolved_heights=resolved_heights)
    assert ok_indices == []
    assert resolved_heights == {}


def test_cut_notches_socket_is_a_true_cylinder():
    top_z = 20.0
    radius, depth = FLORA_NOTCH_RADIUS_MM, 5.0
    verts, faces, protected = _elevated_grid_mesh(6, 60.0, top_z)
    notch = {"x": 30.0, "y": 30.0, "radius_mm": radius, "depth_mm": depth}
    tree_pads.cut_notches(verts, faces, protected, [notch])
    # No original-surface vertex should remain strictly inside the socket
    # mouth — that area's triangles were removed, not just flattened.
    mouth_interior = [
        v for v in verts
        if math.hypot(v[0] - 30.0, v[1] - 30.0) < radius - 1e-6
        and v[2] == pytest.approx(top_z, abs=1e-6)
    ]
    assert mouth_interior == []
    # The socket floor exists at exactly `depth` below the surface.
    floor_z = top_z - depth
    floor_verts = [v for v in verts if v[2] == pytest.approx(floor_z, abs=1e-6)]
    assert len(floor_verts) >= 4   # boundary ring + interior floor verts


def test_cut_notches_skips_gracefully_when_too_coarse(monkeypatch):
    # Force zero refinement passes so a coarse grid can never resolve a tiny
    # notch radius — the cut must be skipped with a warning, not corrupt the
    # mesh or raise.
    monkeypatch.setattr(tree_pads, "NOTCH_MAX_LEVELS", 0)
    verts, faces, protected = _grid_mesh(2, 40.0)
    notch = {"x": 20.0, "y": 20.0, "radius_mm": FLORA_NOTCH_RADIUS_MM,
             "depth_mm": 5.0}
    warnings = []
    new_faces = tree_pads.cut_notches(verts, faces, protected, [notch],
                                      warnings=warnings)
    assert len(warnings) == 1
    assert "too coarse" in warnings[0]
    assert sorted(new_faces) == sorted(faces)   # untouched — a safe no-op


def test_cut_notches_skips_when_too_close_to_rim():
    verts, faces, protected = _grid_mesh(6, 60.0)
    # Centre the notch exactly on a protected boundary vertex.
    notch = {"x": 0.0, "y": 0.0, "radius_mm": FLORA_NOTCH_RADIUS_MM,
             "depth_mm": 5.0}
    warnings = []
    new_faces = tree_pads.cut_notches(verts, faces, protected, [notch],
                                      warnings=warnings)
    assert len(warnings) == 1
    assert "rim" in warnings[0]
    _assert_crack_free(new_faces, protected)


def test_cut_notches_skips_when_floor_too_shallow():
    verts, faces, protected = _grid_mesh(6, 60.0)
    # Push the whole patch down so its z sits just above the min-floor
    # clearance, leaving no room for a 3mm-deep cut.
    verts = [(x, y, FLORA_NOTCH_MIN_FLOOR_MM + 1.0) for (x, y, _z) in verts]
    thin_notch = [{"x": 30.0, "y": 30.0, "radius_mm": FLORA_NOTCH_RADIUS_MM,
                  "depth_mm": 3.0}]
    warnings = []
    new_faces = tree_pads.cut_notches(verts, faces, protected, thin_notch,
                                      warnings=warnings)
    assert len(warnings) == 1
    assert "too thin" in warnings[0]
    _assert_crack_free(new_faces, protected)


def test_notch_skipped_gracefully_on_a_thin_full_tile():
    # A real, minimally-thick tile — the notch's floor would land below z=0,
    # so build_hex_tile must still produce a valid manifold mesh, just
    # without that socket cut, rather than raising or corrupting geometry.
    verts, faces = build_hex_tile(
        diameter_mm=220.0, level_height_mm=10.0, base_thickness_mm=8.2,
        corner_levels=(0, 0, 0, 0, 0, 0), center_level=None,
        smoothness_passes=3,
        flora_pads=[{"x": 0.0, "y": 0.0, "radius_mm": 8.0, "blend_mm": 5.0}],
        flora_notches=CENTER_NOTCH,
    )
    assert_two_manifold(verts, faces)


def test_notch_top_prefix_stable_and_locally_bounded():
    # Notch cutting only ever touches vertices within its own radius (a snap
    # to the exact circle for boundary-loop verts, a sink to the socket floor
    # for interior ones) — count/order of the top-vertex prefix is preserved,
    # and anything beyond the notch radius must be bit-identical to the
    # padded-but-unnotched build, exactly like `refine_and_flatten`'s own
    # "beyond blend reach" contract.
    num_top = top_vertex_count(3, 0)
    pad = {"x": 0.0, "y": 0.0, "radius_mm": 8.0, "blend_mm": 5.0}
    notch = CENTER_NOTCH[0]
    verts_padded, _ = _sloped_tile(flora_pads=[pad])
    verts_notched, _ = _sloped_tile(flora_pads=[pad], flora_notches=CENTER_NOTCH)
    assert len(verts_notched) >= len(verts_padded)
    for i in range(num_top):
        x, y, z = verts_padded[i]
        if math.hypot(x - notch["x"], y - notch["y"]) > notch["radius_mm"] + 1e-6:
            assert verts_notched[i] == pytest.approx((x, y, z), abs=1e-9)


def test_cut_notches_is_deterministic():
    a = _sloped_tile(flora_pads=[{"x": 0.0, "y": 0.0, "radius_mm": 8.0, "blend_mm": 5.0}],
                     flora_notches=CENTER_NOTCH)
    b = _sloped_tile(flora_pads=[{"x": 0.0, "y": 0.0, "radius_mm": 8.0, "blend_mm": 5.0}],
                     flora_notches=CENTER_NOTCH)
    assert a[0] == pytest.approx(list(b[0]), abs=1e-12)
    assert a[1] == b[1]


def test_pin_fits_inside_its_notch():
    # Cheap regression guard: a future constant edit can't silently make the
    # peg wider or longer than the socket it needs to seat into.
    assert FLORA_PIN_RADIUS_MM < FLORA_NOTCH_RADIUS_MM
    assert FLORA_PIN_LENGTH_MM < FLORA_NOTCH_DEPTH_MM


# ---------------------------------------------------------------------------
# 12. `_boundary_loop` — direct unit tests of the riskiest new logic (pinch
# points / multiple disjoint loops are hard to coax out of the real
# refinement pipeline deterministically, so exercise the helper directly).

def test_boundary_loop_single_triangle():
    loop = tree_pads._boundary_loop([(0, 1, 2)])
    assert loop is not None
    assert set(loop) == {0, 1, 2}
    assert len(loop) == 3


def test_boundary_loop_rejects_pinch_point():
    # Two triangles sharing exactly one vertex (not an edge) — that shared
    # vertex has boundary degree 4, not 2.
    removed = [(0, 1, 2), (2, 3, 4)]
    assert tree_pads._boundary_loop(removed) is None


def test_boundary_loop_rejects_disjoint_loops():
    # Two fully separate triangles — a valid loop each, but not ONE loop.
    removed = [(0, 1, 2), (3, 4, 5)]
    assert tree_pads._boundary_loop(removed) is None


# ---------------------------------------------------------------------------
# 13. terrain_pads kwarg — same pipeline slot as flora_pads, merged into one
# refine_and_flatten call, with an explicit "z" target instead of a sampled
# one (see mesh_builder.build_hex_tile's terrain_pads docstring).

def test_pad_z_override_flattens_to_explicit_height_not_sampled():
    verts, faces, protected = _grid_mesh(6, 60.0)
    target_z = 42.0
    pads = [{"x": 30.0, "y": 30.0, "radius_mm": 5.0, "blend_mm": 0.0, "z": target_z}]
    new_faces = tree_pads.refine_and_flatten(
        verts, faces, protected, pads, diameter_mm=1000.0, base_thickness_mm=-1e9)
    inside = [v for v in verts
              if math.hypot(v[0] - 30.0, v[1] - 30.0) <= 5.0 - 1e-6]
    assert len(inside) >= 1
    for v in inside:
        assert v[2] == pytest.approx(target_z, abs=1e-9)


def test_terrain_pads_kwarg_produces_manifold_tile():
    pad = {"x": 0.0, "y": 0.0, "radius_mm": 10.0, "blend_mm": 5.0, "z": 25.0}
    verts, faces = _sloped_tile(terrain_pads=[pad])
    assert_two_manifold(verts, faces)
    inside = [v for v in verts
              if math.hypot(v[0], v[1]) <= pad["radius_mm"] - 1e-6
              and v[2] >= 10.0 - 1e-6]
    assert len(inside) >= 3
    for v in inside:
        assert v[2] == pytest.approx(25.0, abs=1e-6)


def test_flora_and_terrain_pads_merge_into_one_refinement_pass():
    flora_pad = {"x": -20.0, "y": 0.0, "radius_mm": 6.0, "blend_mm": 3.0}
    terrain_pad = {"x": 20.0, "y": 0.0, "radius_mm": 6.0, "blend_mm": 3.0, "z": 30.0}
    verts_both, faces_both = _sloped_tile(
        flora_pads=[flora_pad], terrain_pads=[terrain_pad])
    verts_flora_only, _ = _sloped_tile(flora_pads=[flora_pad])
    verts_terrain_only, _ = _sloped_tile(terrain_pads=[terrain_pad])
    verts_plain, _ = _sloped_tile()

    assert_two_manifold(verts_both, faces_both)
    extra_both = len(verts_both) - len(verts_plain)
    extra_flora = len(verts_flora_only) - len(verts_plain)
    extra_terrain = len(verts_terrain_only) - len(verts_plain)
    # Two well-separated pads refine independently, so combined growth should
    # match the sum of each pad refining alone.
    assert extra_both == extra_flora + extra_terrain

    terrain_inside = [v for v in verts_both
                       if math.hypot(v[0] - 20.0, v[1]) <= terrain_pad["radius_mm"] - 1e-6
                       and v[2] >= 10.0 - 1e-6]
    assert len(terrain_inside) >= 1
    for v in terrain_inside:
        assert v[2] == pytest.approx(30.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Path Feature — curvilinear_coords / sample_grayscale (pure math).

def test_curvilinear_coords_on_segment_midpoint():
    s, t = tree_pads.curvilinear_coords(5.0, 0.0, [(0.0, 0.0), (10.0, 0.0)])
    assert s == pytest.approx(5.0)
    assert t == pytest.approx(0.0, abs=1e-9)


def test_curvilinear_coords_off_to_the_side():
    s, t = tree_pads.curvilinear_coords(5.0, 3.0, [(0.0, 0.0), (10.0, 0.0)])
    assert s == pytest.approx(5.0)
    assert t == pytest.approx(3.0)


def test_curvilinear_coords_other_side_is_negative():
    s, t = tree_pads.curvilinear_coords(5.0, -3.0, [(0.0, 0.0), (10.0, 0.0)])
    assert t == pytest.approx(-3.0)


def test_curvilinear_coords_past_endpoint_clamps():
    s, t = tree_pads.curvilinear_coords(20.0, 0.0, [(0.0, 0.0), (10.0, 0.0)])
    assert s == pytest.approx(10.0)
    s2, t2 = tree_pads.curvilinear_coords(-5.0, 0.0, [(0.0, 0.0), (10.0, 0.0)])
    assert s2 == pytest.approx(0.0)


def test_curvilinear_coords_multi_segment_accumulates_arc_length():
    points = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    s, t = tree_pads.curvilinear_coords(10.0, 5.0, points)
    assert s == pytest.approx(15.0)
    assert t == pytest.approx(0.0, abs=1e-9)


def test_sample_grayscale_bilinear_interpolation():
    # 2x2 texture, both rows [0.0, 1.0] — dead centre averages all 4 corners.
    pixels = [0.0, 1.0, 0.0, 1.0]
    val = tree_pads.sample_grayscale(pixels, 2, 2, 0.5, 0.5)
    assert val == pytest.approx(0.5, abs=1e-6)


def test_sample_grayscale_u_wraps():
    pixels = [0.0, 1.0]
    v0 = tree_pads.sample_grayscale(pixels, 2, 1, 0.25, 0.5)
    v1 = tree_pads.sample_grayscale(pixels, 2, 1, 1.25, 0.5)
    assert v1 == pytest.approx(v0, abs=1e-6)


def test_sample_grayscale_v_clamps_no_wrap():
    pixels = [0.0, 1.0]  # height=2, single column: row0=0.0, row1=1.0
    low = tree_pads.sample_grayscale(pixels, 1, 2, 0.5, -0.5)
    high = tree_pads.sample_grayscale(pixels, 1, 2, 0.5, 1.5)
    assert low == pytest.approx(0.0, abs=1e-6)
    assert high == pytest.approx(1.0, abs=1e-6)


def test_sample_grayscale_degenerate_1x1_texture():
    val = tree_pads.sample_grayscale([0.7], 1, 1, 0.9, 0.1)
    assert val == pytest.approx(0.7, abs=1e-6)


# ---------------------------------------------------------------------------
# Path Feature — refine_and_displace_along_path.

CENTER_PATH = [{
    "points": [(-30.0, 0.0), (30.0, 0.0)],
    "width_mm": 10.0, "depth_mm": 2.0, "blend_mm": 5.0,
    "repeat_mm": 20.0, "pixels": None, "tex_width": 0, "tex_height": 0,
}]


def test_path_feature_tile_is_manifold():
    verts, faces = _sloped_tile(path_features=CENTER_PATH)
    assert_two_manifold(verts, faces)


def test_refine_and_displace_along_path_grid_is_crack_free_and_protects_boundary():
    verts, faces, protected = _grid_mesh(6, 60.0)
    paths = [{
        "points": [(10.0, 30.0), (50.0, 30.0)],
        "width_mm": 10.0, "depth_mm": 2.0, "blend_mm": 3.0,
        "repeat_mm": 20.0, "pixels": None, "tex_width": 0, "tex_height": 0,
    }]
    new_faces = tree_pads.refine_and_displace_along_path(
        verts, faces, protected, paths, diameter_mm=1000.0, base_thickness_mm=-1e9)
    _assert_crack_free(new_faces, protected)
    referenced = set()
    for f in new_faces:
        referenced.update(f)
    assert referenced == set(range(len(verts))), "orphan vertex after refinement"


def test_path_feature_top_prefix_unchanged():
    num_top = top_vertex_count(3, 0)
    verts_plain, _ = _sloped_tile()
    verts_path, _ = _sloped_tile(path_features=CENTER_PATH)
    assert len(verts_path) >= num_top
    for i in range(num_top):
        assert verts_path[i][:2] == pytest.approx(verts_plain[i][:2], abs=1e-9)


def test_no_path_features_adds_zero_vertices():
    verts_plain, _ = _sloped_tile()
    verts_none, _ = _sloped_tile(path_features=None)
    verts_empty, _ = _sloped_tile(path_features=[])
    assert len(verts_none) == len(verts_plain)
    assert len(verts_empty) == len(verts_plain)


def test_path_feature_verts_beyond_reach_unchanged():
    path = CENTER_PATH[0]
    half = path["width_mm"] / 2.0
    reach = half + path["blend_mm"]
    num_top = top_vertex_count(3, 0)
    verts_plain, _ = _sloped_tile()
    verts_path, _ = _sloped_tile(path_features=CENTER_PATH)
    for i in range(num_top):
        x, y, z = verts_plain[i]
        s, t = tree_pads.curvilinear_coords(x, y, path["points"])
        if abs(t) > reach + 1e-6:
            assert verts_path[i] == pytest.approx((x, y, z), abs=1e-9)


def test_path_feature_no_texture_fallback_is_full_depth_groove():
    # A flat (all-equal-corner-level) tile raised well above base_thickness_mm
    # so the groove has real room to carve into without hitting the "top
    # can't go below base_thickness_mm" safety floor.
    base_thickness = 10.0
    flat_z = base_thickness + 3 * 10.0
    depth = 2.0
    half = 5.0
    path = {
        "points": [(-30.0, 0.0), (30.0, 0.0)],
        "width_mm": half * 2.0, "depth_mm": depth, "blend_mm": 3.0,
        "repeat_mm": 20.0, "pixels": None, "tex_width": 0, "tex_height": 0,
    }
    verts, _ = _tile(
        corner_levels=(3, 3, 3, 3, 3, 3), center_level=None,
        base_thickness_mm=base_thickness, smoothness_passes=3,
        path_features=[path])
    inside = [v for v in verts
              if v[2] >= base_thickness - 1e-6
              and abs(v[1]) <= half - 0.5
              and abs(v[0]) <= 20.0]
    assert len(inside) >= 3, "expected several verts inside the path's core width"
    for v in inside:
        assert v[2] == pytest.approx(flat_z - depth, abs=1e-3)


def test_path_feature_white_texture_raises():
    base_thickness = 10.0
    flat_z = base_thickness + 3 * 10.0
    depth = 2.0
    half = 5.0
    path = {
        "points": [(-30.0, 0.0), (30.0, 0.0)],
        "width_mm": half * 2.0, "depth_mm": depth, "blend_mm": 3.0,
        "repeat_mm": 20.0, "pixels": [1.0, 1.0, 1.0, 1.0],
        "tex_width": 2, "tex_height": 2,
    }
    verts, _ = _tile(
        corner_levels=(3, 3, 3, 3, 3, 3), center_level=None,
        base_thickness_mm=base_thickness, smoothness_passes=3,
        path_features=[path])
    inside = [v for v in verts
              if v[2] >= base_thickness - 1e-6
              and abs(v[1]) <= half - 0.5
              and abs(v[0]) <= 20.0]
    assert len(inside) >= 3
    for v in inside:
        assert v[2] == pytest.approx(flat_z + depth, abs=1e-3)


def test_path_feature_near_rim_leaves_rim_corners_at_analytic_height():
    R = 220.0 / 2.0
    diameter = 220.0
    lh = 10.0
    base = 10.0
    levels = (0, 1, 2, 3, 4, 5)
    path = {
        "points": [(R - 20.0, 0.0), (R - 6.0, 0.0)],
        "width_mm": 6.0, "depth_mm": 3.0, "blend_mm": 5.0,
        "repeat_mm": 20.0, "pixels": None, "tex_width": 0, "tex_height": 0,
    }
    verts, faces = build_hex_tile(
        diameter_mm=diameter, level_height_mm=lh, base_thickness_mm=base,
        corner_levels=levels, center_level=None, smoothness_passes=3,
        path_features=[path],
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
