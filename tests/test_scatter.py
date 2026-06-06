"""Tests for the bpy-free half of the *scatter* surface kind (Boulder Field).

Placement planning, the boulder mesh, and the joined-mesh assembler are all pure
geometry math in `procedural_surfaces`, so they're held to the same standard as
the displacement surfaces: deterministic, manifold, scale-correct. The bpy-only
parts (object lifecycle, raycast seating, boolean merge) live in `scatter.py` and
are covered by the manual checklist in docs/boulder-field.md.
"""

import math

import pytest

import procedural_surfaces as ps
from manifold_check import assert_two_manifold


SQUARE = [(-200.0, -200.0), (200.0, -200.0), (200.0, 200.0), (-200.0, 200.0)]


def _scatter(**kw):
    base = dict(min_size_mm=10.0, max_size_mm=60.0, density=0.6,
                distribution=0.5, seed=3)
    base.update(kw)
    return ps.scatter_boulders(SQUARE, **base)


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------
def test_placement_deterministic():
    assert _scatter() == _scatter()


def test_placement_seed_changes_layout():
    assert _scatter(seed=1) != _scatter(seed=2)


def test_placement_density_monotonic():
    sparse = _scatter(density=0.1)
    packed = _scatter(density=0.9)
    assert len(packed) > len(sparse)


def test_placement_degenerate_polygon_is_empty():
    assert ps.scatter_boulders([(0.0, 0.0), (1.0, 1.0)], min_size_mm=10.0,
                               max_size_mm=60.0, density=0.6,
                               distribution=0.5, seed=1) == []


def test_placement_centres_inside_polygon():
    for (x, y, _r, _rot, _pid) in _scatter():
        assert ps.point_in_polygon(x, y, SQUARE)


def test_placement_radii_within_size_bounds():
    lo, hi = 10.0, 60.0
    for (_x, _y, r, _rot, _pid) in _scatter(min_size_mm=lo, max_size_mm=hi):
        assert lo / 2.0 - 1e-9 <= r <= hi / 2.0 + 1e-9


def test_placement_swapped_bounds_are_normalised():
    lo, hi = 10.0, 60.0
    for (_x, _y, r, _rot, _pid) in _scatter(min_size_mm=hi, max_size_mm=lo):
        assert lo / 2.0 - 1e-9 <= r <= hi / 2.0 + 1e-9


def test_placement_broad_range_makes_large_and_small():
    lo, hi = 10.0, 200.0
    sizes = [2.0 * r for (_x, _y, r, _rot, _pid) in
             _scatter(min_size_mm=lo, max_size_mm=hi, density=0.9)]
    span = hi - lo
    assert min(sizes) < lo + 0.3 * span    # some small
    assert max(sizes) > lo + 0.7 * span    # some large


def test_placement_rotation_in_range():
    for (_x, _y, _r, rot, _pid) in _scatter():
        assert 0.0 <= rot <= 2.0 * math.pi


def test_distribution_shifts_size_histogram():
    # Uniform sizes (distribution=1) have a larger mean than the small-biased
    # distribution=0 over the same cells.
    def mean_size(dist):
        ps_ = _scatter(distribution=dist)
        return sum(2.0 * r for (_x, _y, r, _rot, _pid) in ps_) / len(ps_)
    assert mean_size(1.0) > mean_size(0.0)


# ---------------------------------------------------------------------------
# Boulder mesh
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("subdiv", [0, 1, 2])
def test_icosphere_is_manifold(subdiv):
    verts, faces = ps._icosphere(subdiv)
    assert_two_manifold(verts, faces)
    # Euler characteristic of a sphere: V - E + F == 2 (triangles -> E = 3F/2).
    assert len(verts) - (3 * len(faces) // 2) + len(faces) == 2


def test_boulder_mesh_is_manifold():
    verts, faces = ps.boulder_mesh(25.0, 7)
    assert_two_manifold(verts, faces)


def test_boulder_vertex_radius_within_bounds():
    radius, roughness = 25.0, 0.35
    verts, _ = ps.boulder_mesh(radius, 7, roughness=roughness)
    lo = radius * (1.0 - roughness / 2.0)
    hi = radius * (1.0 + roughness / 2.0)
    for (x, y, z) in verts:
        d = math.sqrt(x * x + y * y + z * z)
        assert lo - 1e-6 <= d <= hi + 1e-6


def test_boulder_mesh_deterministic_per_pid():
    assert ps.boulder_mesh(25.0, 7)[0] == ps.boulder_mesh(25.0, 7)[0]


def test_boulder_mesh_differs_by_pid():
    assert ps.boulder_mesh(25.0, 7)[0] != ps.boulder_mesh(25.0, 8)[0]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def test_assembly_is_manifold():
    placements = _scatter()
    verts, faces = ps.assemble_scatter_mesh(placements, lambda x, y: 5.0)
    assert_two_manifold(verts, faces)


def test_assembly_counts_are_sum_of_boulders():
    placements = _scatter()
    verts, faces = ps.assemble_scatter_mesh(placements, lambda x, y: 0.0,
                                            subdiv=1)
    per_v, per_f = ps._icosphere(1)
    assert len(verts) == len(placements) * len(per_v)
    assert len(faces) == len(placements) * len(per_f)


def test_assembly_z_of_offsets_applied():
    placements = _scatter()
    flat, _ = ps.assemble_scatter_mesh(placements, lambda x, y: 0.0)
    lifted, _ = ps.assemble_scatter_mesh(placements, lambda x, y: 10.0)
    assert len(flat) == len(lifted)
    for (fx, fy, fz), (lx, ly, lz) in zip(flat, lifted):
        assert lx == pytest.approx(fx)
        assert ly == pytest.approx(fy)
        assert lz == pytest.approx(fz + 10.0)


def test_assembly_empty_is_empty():
    verts, faces = ps.assemble_scatter_mesh([], lambda x, y: 0.0)
    assert verts == [] and faces == []


# ---------------------------------------------------------------------------
# Registry — scatter kind integrates without touching the displace contract
# ---------------------------------------------------------------------------
def test_boulders_is_a_scatter_surface():
    assert ps.surface_kind("BOULDERS") == 'scatter'
    assert ps.SURFACES["BOULDERS"].generator is None
    assert ps.SURFACES["BOULDERS"].placement_fn is ps.scatter_boulders
    assert ps.SURFACES["BOULDERS"].element_mesh_fn is ps.boulder_mesh


def test_scatter_excluded_from_generating_fanout():
    generating = [k for k, s in ps.SURFACES.items() if s.generator is not None]
    assert "BOULDERS" not in generating


def test_scatter_contributes_zero_displacement():
    assert ps.surface_offset(3.0, 4.0, surface_type="BOULDERS", feature_mm=10.0,
                             depth_mm=5.0, regularity=0.5, seed=1) == 0.0


def test_existing_surfaces_are_displace_kind():
    for key in ("NONE", "COBBLESTONE", "GRAVEL", "FURROW"):
        assert ps.surface_kind(key) == 'displace'


def test_extra_param_defaults_keys_and_values():
    d = ps.extra_param_defaults("BOULDERS", 1800.0)
    assert set(d) == {"min_size_mm", "max_size_mm", "density", "distribution"}
    # mm params equal their reference at real man-height; factors are verbatim.
    assert d["min_size_mm"] == pytest.approx(80.0)
    assert d["max_size_mm"] == pytest.approx(400.0)
    assert d["density"] == pytest.approx(0.6)
    assert d["distribution"] == pytest.approx(0.3)


def test_extra_param_mm_defaults_scale_with_man_height():
    d = ps.extra_param_defaults("BOULDERS", 10.0)
    assert d["min_size_mm"] == pytest.approx(80.0 * 10.0 / 1800.0)
    assert d["max_size_mm"] == pytest.approx(400.0 * 10.0 / 1800.0)
    # factor params do NOT scale.
    assert d["density"] == pytest.approx(0.6)


def test_extra_param_specs_empty_for_displace():
    assert ps.extra_param_specs("COBBLESTONE") == ()
    assert ps.extra_param_defaults("COBBLESTONE", 10.0) == {}


def test_polygon_area_square():
    assert ps.polygon_area(SQUARE) == pytest.approx(400.0 * 400.0)


def test_estimate_boulder_count_tracks_actual_order_of_magnitude():
    kw = dict(min_size_mm=20.0, max_size_mm=60.0, density=0.6)
    est = ps.estimate_boulder_count(SQUARE, **kw)
    actual = len(ps.scatter_boulders(SQUARE, distribution=0.5, seed=3, **kw))
    assert est > 0
    # Estimate ignores the inside-polygon test, so it should be within ~2x.
    assert 0.4 * est <= actual <= 1.6 * est


def test_estimate_boulder_count_monotonic_in_density():
    kw = dict(min_size_mm=20.0, max_size_mm=60.0)
    assert (ps.estimate_boulder_count(SQUARE, density=0.9, **kw)
            > ps.estimate_boulder_count(SQUARE, density=0.1, **kw))


def test_estimate_boulder_count_degenerate_is_zero():
    assert ps.estimate_boulder_count([(0.0, 0.0)], min_size_mm=10.0,
                                     max_size_mm=20.0, density=0.5) == 0


def test_hex_polygon_matches_diameter():
    poly = ps.hex_polygon(220.0)
    assert len(poly) == 6
    for (x, y) in poly:
        assert math.hypot(x, y) == pytest.approx(110.0)
