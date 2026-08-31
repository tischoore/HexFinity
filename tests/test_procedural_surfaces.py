"""Tests for the bpy-free procedural surface generator.

The contract-level tests are PARAMETRISED OVER THE REGISTRY, so every surface —
including any added later — is automatically held to the same invariants
(determinism, bounded amplitude, scale, varied output). That fan-out is the
machine-checked half of the "easily extensible" guarantee: register a surface
and it inherits the whole suite.
"""

import math

import pytest

import procedural_surfaces as ps
from mesh_builder import build_hex_tile, top_vertex_count
from manifold_check import assert_two_manifold


# Every surface that actually generates geometry (i.e. not NONE).
GENERATING = [k for k, s in ps.SURFACES.items() if s.generator is not None]

PARAMS = dict(feature_mm=20.0, depth_mm=2.0, regularity=0.5, seed=7)


def _offset(stype, x, y, **kw):
    p = dict(PARAMS, **kw)
    return ps.surface_offset(x, y, surface_type=stype, **p)


# ---------------------------------------------------------------------------
# Registry / scale model
# ---------------------------------------------------------------------------
def test_none_is_first_and_a_noop():
    assert next(iter(ps.SURFACES)) == "NONE"
    assert _offset("NONE", 3.0, 4.0) == 0.0


def test_enum_items_track_registry():
    keys = [item[0] for item in ps.enum_items()]
    assert keys == list(ps.SURFACES.keys())
    # 3-tuples (id, label, description) as Blender expects.
    assert all(len(item) == 3 for item in ps.enum_items())


@pytest.mark.parametrize("stype", GENERATING)
def test_feature_mm_default_scales_with_man_height(stype):
    ref = ps.SURFACES[stype].reference_mm
    assert ps.feature_mm_default(stype, 1800.0) == pytest.approx(ref)
    assert ps.feature_mm_default(stype, 10.0) == pytest.approx(ref * 10.0 / 1800.0)


def test_feature_mm_default_none_is_zero():
    assert ps.feature_mm_default("NONE", 10.0) == 0.0


def test_unknown_surface_is_safe():
    assert _offset("DOES_NOT_EXIST", 1.0, 2.0) == 0.0


# ---------------------------------------------------------------------------
# Contract every generating surface must satisfy (auto-covers new surfaces)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stype", GENERATING)
def test_deterministic(stype):
    a = _offset(stype, 1.234, 5.678)
    b = _offset(stype, 1.234, 5.678)
    assert a == b


@pytest.mark.parametrize("stype", GENERATING)
def test_seed_changes_pattern(stype):
    a = _offset(stype, 1.234, 5.678, seed=1)
    b = _offset(stype, 1.234, 5.678, seed=2)
    assert a != b


@pytest.mark.parametrize("stype", GENERATING)
def test_bounded_by_depth(stype):
    depth = PARAMS["depth_mm"]
    for i in range(400):
        x = (i * 1.7) % 137.0
        y = (i * 2.9) % 211.0
        assert abs(_offset(stype, x, y)) <= depth + 1e-9


@pytest.mark.parametrize("stype", GENERATING)
def test_zero_depth_or_feature_is_noop(stype):
    assert _offset(stype, 9.0, 9.0, depth_mm=0.0) == 0.0
    assert _offset(stype, 9.0, 9.0, feature_mm=0.0) == 0.0


@pytest.mark.parametrize("stype", GENERATING)
def test_produces_variation(stype):
    vals = [_offset(stype, x * 0.7, x * 1.3) for x in range(500)]
    assert max(vals) - min(vals) > 0.1  # not a flat surface


@pytest.mark.parametrize("stype", GENERATING)
def test_origin_offset_shifts_pattern(stype):
    # Sampling at (x,y) with an origin equals sampling the shifted global point.
    here = _offset(stype, 0.0, 0.0, origin_xy=(50.0, 50.0))
    there = _offset(stype, 50.0, 50.0, origin_xy=(0.0, 0.0))
    assert here == pytest.approx(there)


# ---------------------------------------------------------------------------
# Cobblestone-specific: feature spacing tracks feature_mm (scale correctness)
# ---------------------------------------------------------------------------
def test_cobblestone_cell_pitch_matches_feature_mm():
    # Count distinct nearest-cells along a long scan line; spacing ~ feature_mm.
    feature = 10.0
    span = 500.0
    n = 2000
    cells = set()
    for i in range(n):
        x = span * i / n
        _, _, cell = ps._worley(x, 0.0, feature, 0.4, seed=3)
        cells.add(cell)
    # Roughly span / feature distinct columns crossed (allow generous slack).
    expected = span / feature
    assert 0.5 * expected <= len(cells) <= 2.0 * expected


# ---------------------------------------------------------------------------
# Direction (anisotropy) — furrows must orient to direction_rad
# ---------------------------------------------------------------------------
def test_furrow_is_constant_along_its_direction():
    # regularity=1 removes wander, so the ridge value is constant along the
    # furrow direction and varies across it.
    kw = dict(feature_mm=20.0, depth_mm=2.0, regularity=1.0, seed=4)
    ang = math.radians(35.0)
    c, s = math.cos(ang), math.sin(ang)
    base = ps.surface_offset(0.0, 0.0, surface_type="FURROW",
                             direction_rad=ang, **kw)
    # Move ALONG the direction -> unchanged.
    along = ps.surface_offset(50.0 * c, 50.0 * s, surface_type="FURROW",
                              direction_rad=ang, **kw)
    assert along == pytest.approx(base, abs=1e-9)
    # Move ACROSS the direction by a quarter pitch -> changed.
    across = ps.surface_offset(-5.0 * s, 5.0 * c, surface_type="FURROW",
                               direction_rad=ang, **kw)
    assert across != pytest.approx(base, abs=1e-6)


def test_isotropic_surfaces_ignore_direction():
    kw = dict(feature_mm=20.0, depth_mm=2.0, regularity=0.5, seed=4)
    for stype in ("COBBLESTONE", "GRAVEL"):
        a = ps.surface_offset(7.0, 3.0, surface_type=stype, direction_rad=0.0, **kw)
        b = ps.surface_offset(7.0, 3.0, surface_type=stype,
                              direction_rad=1.3, **kw)
        assert a == b


# ---------------------------------------------------------------------------
# Plains-specific: regularity trades high-frequency roughness for smoothness
# ---------------------------------------------------------------------------
def test_plains_regularity_smooths_the_result():
    # Point-to-point variation along a scan line is a cheap roughness proxy;
    # regularity=1 (high persistence decay) should be noticeably smoother
    # than regularity=0 (more high-frequency octave energy).
    def roughness(regularity):
        kw = dict(feature_mm=20.0, depth_mm=2.0, regularity=regularity, seed=9)
        vals = [ps.surface_offset(x * 0.5, 0.0, surface_type="PLAINS", **kw)
                for x in range(200)]
        return sum(abs(vals[i] - vals[i - 1]) for i in range(1, len(vals)))

    assert roughness(0.0) > roughness(1.0)


# ---------------------------------------------------------------------------
# Region masking — point-in-polygon + soft boundary falloff
# ---------------------------------------------------------------------------
SQUARE = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]


def test_point_in_polygon_basic():
    assert ps.point_in_polygon(50.0, 50.0, SQUARE)
    assert not ps.point_in_polygon(150.0, 50.0, SQUARE)
    assert not ps.point_in_polygon(-1.0, 50.0, SQUARE)


def test_region_mask_inside_outside_and_falloff():
    # Deep inside -> 1, outside -> 0.
    assert ps.region_mask(50.0, 50.0, SQUARE, falloff_mm=10.0) == pytest.approx(1.0)
    assert ps.region_mask(200.0, 50.0, SQUARE, falloff_mm=10.0) == 0.0
    # Within the falloff band -> strictly between 0 and 1.
    edge = ps.region_mask(2.0, 50.0, SQUARE, falloff_mm=10.0)
    assert 0.0 < edge < 1.0


def test_region_mask_zero_falloff_is_hard_edge():
    assert ps.region_mask(1.0, 50.0, SQUARE, falloff_mm=0.0) == 1.0
    assert ps.region_mask(-1.0, 50.0, SQUARE, falloff_mm=0.0) == 0.0


def test_region_mask_degenerate_polygon():
    assert ps.region_mask(0.0, 0.0, [(0.0, 0.0), (1.0, 1.0)], falloff_mm=5.0) == 0.0


def test_polygon_edge_distance():
    assert ps.polygon_edge_distance(50.0, 5.0, SQUARE) == pytest.approx(5.0)
    assert ps.polygon_edge_distance(50.0, 50.0, SQUARE) == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Builder integration — a region textures only its interior, manifold holds
# ---------------------------------------------------------------------------
def _flat_tile(**kw):
    base = dict(
        diameter_mm=220.0, level_height_mm=10.0, base_thickness_mm=10.0,
        corner_levels=(0, 0, 0, 0, 0, 0), center_level=None,
        smoothness_passes=2, resample_density=0,
    )
    base.update(kw)
    return build_hex_tile(**base)


# A square region centred on the tile, well away from the rim (apothem ~95 mm).
CENTER_REGION = [{
    "surface_type": "COBBLESTONE", "feature_mm": 12.0, "depth_mm": 4.0,
    "regularity": 0.4, "direction_rad": 0.0,
    "polygon": [(-30.0, -30.0), (30.0, -30.0), (30.0, 30.0), (-30.0, 30.0)],
    "mask_falloff_mm": 5.0,
}]


def test_region_builds_a_manifold_tile():
    verts, faces = _flat_tile(surface_regions=CENTER_REGION)
    assert_two_manifold(verts, faces)


def test_region_displaces_interior_not_exterior():
    num_top = top_vertex_count(2, 0)
    flat, _ = _flat_tile()
    tex, _ = _flat_tile(surface_regions=CENTER_REGION)

    moved_inside = moved_outside = 0
    for i in range(num_top):
        x, y, _ = flat[i]
        inside = ps.point_in_polygon(x, y, CENTER_REGION[0]["polygon"])
        changed = abs(tex[i][2] - flat[i][2]) > 1e-9
        if inside:
            moved_inside += changed
        elif abs(x) > 45.0 or abs(y) > 45.0:  # clearly outside + past falloff
            moved_outside += changed
    assert moved_inside > 0       # interior textured
    assert moved_outside == 0     # exterior untouched


def test_region_leaves_nontop_geometry_untouched():
    num_top = top_vertex_count(2, 0)
    flat, _ = _flat_tile()
    tex, _ = _flat_tile(surface_regions=CENTER_REGION)
    assert len(flat) == len(tex)
    for i in range(num_top, len(flat)):
        assert flat[i] == tex[i]   # bottom/side/tab verts identical


def test_region_clamps_to_base_thickness():
    verts, _ = _flat_tile(surface_regions=[{
        "surface_type": "COBBLESTONE", "feature_mm": 12.0, "depth_mm": 50.0,
        "regularity": 0.4, "polygon": CENTER_REGION[0]["polygon"],
        "mask_falloff_mm": 5.0,
    }])
    num_top = top_vertex_count(2, 0)
    for i in range(num_top):
        assert verts[i][2] >= 10.0 - 1e-9   # base_thickness_mm


def test_region_fades_to_flat_at_rim():
    # A whole-tile region (no polygon) must still leave the rim corners exact.
    flat, _ = _flat_tile()
    tex, _ = _flat_tile(surface_regions=[{
        "surface_type": "GRAVEL", "feature_mm": 8.0, "depth_mm": 3.0,
        "regularity": 0.3,
    }])
    R = 110.0  # diameter/2 — the six rim corners sit at radius R
    for i in range(top_vertex_count(2, 0)):
        x, y, _ = flat[i]
        if abs(math.hypot(x, y) - R) < 1e-6:
            assert tex[i][2] == pytest.approx(flat[i][2], abs=1e-9)


def test_worley_neighbourhood_finds_true_nearest():
    # Brute-force nearest over a wide window must match the 3x3 fast path.
    feature, jitter, seed = 12.0, 1.0, 11
    for (x, y) in [(3.0, 7.0), (40.0, -15.0), (123.4, 88.8)]:
        f1, _, _ = ps._worley(x, y, feature, jitter, seed)
        cx = int(math.floor(x / feature))
        cy = int(math.floor(y / feature))
        best = float("inf")
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                px, py = ps._cell_center(cx + dx, cy + dy, feature, jitter, seed)
                best = min(best, math.hypot(x - px, y - py))
        assert f1 == pytest.approx(best)


# ---------------------------------------------------------------------------
# obb_overlap — flora.py's plant-time tree/tree collision test
# ---------------------------------------------------------------------------
def test_obb_overlap_far_apart_boxes_do_not_overlap():
    assert not ps.obb_overlap(0, 0, 5, 5, 0.0, 100, 100, 5, 5, 0.0)


def test_obb_overlap_axis_aligned_overlap():
    # Centers 8mm apart on X, half-widths 5mm each -> 2mm of overlap.
    assert ps.obb_overlap(0, 0, 5, 5, 0.0, 8, 0, 5, 5, 0.0)


def test_obb_overlap_axis_aligned_clear():
    # Centers 12mm apart on X, half-widths 5mm each -> 2mm of clear gap.
    assert not ps.obb_overlap(0, 0, 5, 5, 0.0, 12, 0, 5, 5, 0.0)


def test_obb_overlap_only_detected_once_rotated():
    # A small square at the origin, and a long thin bar 10mm above it.
    # Axis-aligned, the bar's short side faces the square and they clear by
    # 8.5mm; rotated 90 degrees, the bar's long axis swings down into the
    # square. Proves the test actually uses both boxes' own axes.
    square = (0, 0, 1, 1, 0.0)
    bar_flat = (0, 10, 10, 0.5, 0.0)
    bar_upright = (0, 10, 10, 0.5, math.radians(90))
    assert not ps.obb_overlap(*square, *bar_flat)
    assert ps.obb_overlap(*square, *bar_upright)


def test_obb_overlap_touching_boxes_are_not_overlapping():
    # Exactly touching (zero-width gap) must count as clear, not overlapping.
    assert not ps.obb_overlap(0, 0, 5, 5, 0.0, 10, 0, 5, 5, 0.0)


def test_obb_overlap_min_gap_rejects_a_previously_clear_pair():
    # 2mm of clear gap at min_gap=0, but requiring 4mm of clearance closes it.
    assert not ps.obb_overlap(0, 0, 5, 5, 0.0, 12, 0, 5, 5, 0.0)
    assert ps.obb_overlap(0, 0, 5, 5, 0.0, 12, 0, 5, 5, 0.0, min_gap=4.0)


def test_obb_overlap_is_symmetric():
    args_a = (3, -2, 4, 6, math.radians(20))
    args_b = (5, 1, 3, 3, math.radians(-40))
    assert ps.obb_overlap(*args_a, *args_b) == ps.obb_overlap(*args_b, *args_a)
