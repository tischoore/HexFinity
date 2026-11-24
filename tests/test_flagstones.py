"""Tests for the bpy-free half of the *Flagstone Paving* scatter surface.

Placement is a power-diagram (weighted Voronoi) tessellation of the region,
each cell inset along its shared seams for the mortar gap -- see
`procedural_surfaces.scatter_flagstones`'s docstring and
`docs/flagstone-paving.md`. The flagstone mesh and the tilt-fitting joined-
mesh assembler are pure geometry math in `procedural_surfaces`, so they're
held to the same standard as the displacement surfaces and Boulder Field
(test_scatter.py): deterministic, manifold, scale-correct. The bpy-only parts
(object lifecycle, raycast seating, boolean merge) live in `flagstones.py`.
"""

import math

import pytest

import procedural_surfaces as ps
import segment_geometry as sg
from manifold_check import assert_two_manifold


SQUARE = [(-400.0, -400.0), (400.0, -400.0), (400.0, 400.0), (-400.0, 400.0)]


def _scatter(**kw):
    base = dict(min_size_mm=300.0, max_size_mm=500.0, gap_mm=10.0, seed=3)
    base.update(kw)
    return ps.scatter_flagstones(SQUARE, **base)


def _poly_min_dist(poly_a, poly_b):
    """Minimum distance (mm) between the boundaries of two convex polygons
    -- every vertex of one against every edge of the other, both ways. Good
    enough for a geometry regression test, not a general-purpose primitive."""
    best = float("inf")

    def vert_vs_edges(verts, edges_of):
        nonlocal best
        n = len(edges_of)
        for (vx, vy) in verts:
            for i in range(n):
                ax, ay = edges_of[i]
                bx, by = edges_of[(i + 1) % n]
                dx, dy = bx - ax, by - ay
                seg2 = dx * dx + dy * dy
                if seg2 <= 1e-18:
                    d = math.hypot(vx - ax, vy - ay)
                else:
                    t = ((vx - ax) * dx + (vy - ay) * dy) / seg2
                    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                    d = math.hypot(vx - (ax + t * dx), vy - (ay + t * dy))
                if d < best:
                    best = d

    vert_vs_edges(poly_a, poly_b)
    vert_vs_edges(poly_b, poly_a)
    return best


def _world_outline(placement):
    x, y, outline_local, _pid = placement
    return [(x + lx, y + ly) for (lx, ly) in outline_local]


def _vertex_strictly_inside(poly_inner, poly_outer):
    return any(ps.point_in_polygon(x, y, poly_outer) and
              not any(abs(x - ox) < 1e-6 and abs(y - oy) < 1e-6
                      for (ox, oy) in poly_outer)
              for (x, y) in poly_inner)


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------
def test_placement_deterministic():
    assert _scatter() == _scatter()


def test_placement_seed_changes_layout():
    assert _scatter(seed=1) != _scatter(seed=2)


def test_placement_degenerate_polygon_is_empty():
    assert ps.scatter_flagstones([(0.0, 0.0), (1.0, 1.0)], min_size_mm=300.0,
                                 max_size_mm=500.0, gap_mm=10.0, seed=1) == []


def test_placement_zero_max_size_is_empty():
    assert _scatter(min_size_mm=0.0, max_size_mm=0.0) == []


def test_placement_centres_inside_polygon():
    for (x, y, _outline, _pid) in _scatter():
        assert ps.point_in_polygon(x, y, SQUARE)


def test_placement_swapped_bounds_are_normalised():
    # The seed draw (and therefore the resulting layout) must be identical
    # regardless of which bound is passed as min vs. max.
    assert _scatter(min_size_mm=500.0, max_size_mm=300.0) == \
        _scatter(min_size_mm=300.0, max_size_mm=500.0)


def test_placement_outline_has_at_least_a_triangle():
    for (_x, _y, outline, _pid) in _scatter():
        assert len(outline) >= 3


def test_placement_outline_relative_to_its_own_centre():
    # outline_local is relative to (x, y); translating back must land inside
    # (or essentially on the boundary of) the requested polygon.
    for (x, y, outline, _pid) in _scatter():
        for (lx, ly) in outline:
            wx, wy = x + lx, y + ly
            assert ps.point_in_polygon(wx, wy, SQUARE) or \
                ps.polygon_edge_distance(wx, wy, SQUARE) < 1e-3


def test_placement_no_two_outlines_overlap():
    placements = _scatter(min_size_mm=150.0, max_size_mm=350.0)
    worlds = [_world_outline(p) for p in placements]
    assert len(worlds) > 5
    for i, a in enumerate(worlds):
        for b in worlds[i + 1:]:
            assert not _vertex_strictly_inside(a, b)
            assert not _vertex_strictly_inside(b, a)


def test_placement_zero_gap_tessellation_exactly_covers_polygon():
    # With no mortar inset at all, the power diagram must exactly PARTITION
    # the region: the stones' own areas should sum to the full polygon area
    # (the real invariant a true tessellation gives "for free" -- the
    # earlier circle-packing algorithm could never make this claim).
    placements = _scatter(gap_mm=0.0, min_size_mm=150.0, max_size_mm=350.0)
    total = sum(ps.polygon_area(outline) for (_x, _y, outline, _pid) in placements)
    assert total == pytest.approx(ps.polygon_area(SQUARE), rel=1e-2)


def test_placement_zero_gap_stones_touch():
    # Zero gap -> zero inset on every mortar seam -> adjacent stones share
    # (part of) an edge, i.e. touch at distance ~0.
    placements = _scatter(gap_mm=0.0, min_size_mm=150.0, max_size_mm=350.0)
    worlds = [(x, y, _world_outline(p)) for p in placements for (x, y) in [(p[0], p[1])]]
    found_touch = False
    for i, (xi, yi, wi) in enumerate(worlds):
        for (xj, yj, wj) in worlds[i + 1:]:
            if math.hypot(xi - xj, yi - yj) > 400.0:
                continue
            if _poly_min_dist(wi, wj) < 1e-3:
                found_touch = True
                break
        if found_touch:
            break
    assert found_touch


def test_placement_average_gap_close_to_target():
    # Over many neighbouring pairs, the realized gap should average out
    # close to gap_mm -- the user-facing contract this whole algorithm
    # exists for -- even though any single pair may vary.
    gap_mm = 20.0
    placements = _scatter(gap_mm=gap_mm, min_size_mm=150.0, max_size_mm=350.0)
    assert len(placements) > 8
    worlds = [(p[0], p[1], _world_outline(p)) for p in placements]
    gaps = []
    for i, (xi, yi, wi) in enumerate(worlds):
        for (xj, yj, wj) in worlds[i + 1:]:
            if math.hypot(xi - xj, yi - yj) > 350.0:
                continue
            gaps.append(_poly_min_dist(wi, wj))
    assert len(gaps) > 5
    mean_gap = sum(gaps) / len(gaps)
    assert 0.3 * gap_mm <= mean_gap <= 1.7 * gap_mm


def test_placement_allows_touching_with_a_small_average_gap():
    # A small average gap over stones with real size variance should still
    # let some pairs come out touching (gap ~0), not just "uniformly small".
    placements = _scatter(gap_mm=3.0, min_size_mm=150.0, max_size_mm=400.0)
    worlds = [(p[0], p[1], _world_outline(p)) for p in placements]
    min_gap = min(
        _poly_min_dist(wi, wj)
        for i, (xi, yi, wi) in enumerate(worlds)
        for (xj, yj, wj) in worlds[i + 1:]
        if math.hypot(xi - xj, yi - yj) <= 400.0)
    assert min_gap < 1.0


def test_placement_near_full_coverage():
    # Minus the (comparatively small) mortar-joint area, the tessellation
    # should cover most of the region -- a real invariant now, not just a
    # packer's best effort.
    gap_mm = 10.0
    placements = _scatter(gap_mm=gap_mm, min_size_mm=300.0, max_size_mm=500.0)
    total = sum(ps.polygon_area(outline) for (_x, _y, outline, _pid) in placements)
    assert total > 0.75 * ps.polygon_area(SQUARE)


def test_placement_no_oversized_outlier_cell_over_a_large_region():
    # Regression guard for a real bug: a fixed, un-widened neighbour search
    # in `_power_cell` could miss the one seed that should have clipped a
    # cell in a locally sparser-than-average direction (seed jitter routinely
    # produces these), leaving that cell unconstrained on that side and
    # ballooning it out toward the region's own far boundary -- visible as
    # one huge stone floating above an otherwise-correct tessellation. Over
    # many seeds and seed values, no cell should come out many times the
    # area of the mean.
    big_square = [(-3000.0, -3000.0), (3000.0, -3000.0),
                 (3000.0, 3000.0), (-3000.0, 3000.0)]
    for seed in range(20):
        placements = ps.scatter_flagstones(big_square, min_size_mm=420.0,
                                           max_size_mm=650.0, gap_mm=10.0,
                                           seed=seed)
        areas = [ps.polygon_area(o) for (_x, _y, o, _p) in placements]
        assert len(areas) > 20
        mean_area = sum(areas) / len(areas)
        assert max(areas) < 6.0 * mean_area


def test_placement_no_sliver_edges_among_outlines():
    # Regression guard for a real bug: sequential half-plane clipping can
    # leave two near-duplicate vertices behind (two different clip lines
    # crossing the boundary at nearly the same point), producing a
    # razor-thin edge -- most visible on cells that touch the region's own
    # rim. No outline's shortest edge should be a razor-thin sliver relative
    # to its own size.
    big_square = [(-3000.0, -3000.0), (3000.0, -3000.0),
                 (3000.0, 3000.0), (-3000.0, 3000.0)]
    for seed in range(60):
        placements = ps.scatter_flagstones(big_square, min_size_mm=420.0,
                                           max_size_mm=650.0, gap_mm=10.0,
                                           seed=seed)
        for (_x, _y, outline, _pid) in placements:
            n = len(outline)
            scale = math.sqrt(ps.polygon_area(outline))
            if scale <= 0.0:
                continue
            min_edge = min(math.hypot(outline[i][0] - outline[(i + 1) % n][0],
                                      outline[i][1] - outline[(i + 1) % n][1])
                           for i in range(n))
            assert min_edge / scale > 1e-3


def test_placement_rejects_slivers_near_an_acute_boundary_corner():
    # Regression guard for a real bug: a seed landing near a tight/acute
    # corner of a hand-drawn or flood-filled region's own boundary (the
    # hex's own 6 corners are never this acute) gets clipped into a sliver
    # far smaller than its own drawn size -- geometrically a valid
    # tessellation cell, but not a usable paving stone (and, being so
    # small, disproportionately sensitive to local height variation, so it
    # tilts hard against whatever the terrain does at that one spot -- a
    # single oddly-angled sliver among otherwise normal stones). Every
    # placement's area must be at least `_FLAGSTONE_MIN_CELL_AREA_FRACTION`
    # of its own seed's SMALLEST possible nominal area (using the global
    # `min_size_mm` as the floor, since each seed's own threshold can only
    # be at or above that).
    wedge = [(0.0, 0.0), (2000.0, 60.0), (2000.0, -60.0)]
    lo, hi = 150.0, 250.0
    floor = ps._FLAGSTONE_MIN_CELL_AREA_FRACTION * math.pi * (lo / 2.0) ** 2
    total = 0
    for seed in range(30):
        placements = ps.scatter_flagstones(wedge, min_size_mm=lo, max_size_mm=hi,
                                           gap_mm=8.0, seed=seed)
        total += len(placements)
        for (_x, _y, outline, _pid) in placements:
            assert ps.polygon_area(outline) >= floor - 1e-6
    assert total > 20   # the wedge must still have produced real stones


def test_placement_fills_a_larger_region_without_holes():
    # On a region much bigger than one stone, the tessellation should reach
    # every corner, not just cluster near the centroid -- a regression guard
    # for the old packer's "one small blob, rest left empty" failure mode.
    big_square = [(-1500.0, -1500.0), (1500.0, -1500.0),
                 (1500.0, 1500.0), (-1500.0, 1500.0)]
    placements = ps.scatter_flagstones(big_square, min_size_mm=200.0,
                                       max_size_mm=350.0, gap_mm=10.0, seed=5)
    assert len(placements) > 20
    max_extent = max(math.hypot(x, y) for (x, y, _o, _p) in placements)
    assert max_extent > 0.5 * 1500.0
    total = sum(ps.polygon_area(outline) for (_x, _y, outline, _pid) in placements)
    assert total > 0.75 * ps.polygon_area(big_square)


# ---------------------------------------------------------------------------
# _intersect_halfplanes / _power_cell / _inset_cell primitives
# ---------------------------------------------------------------------------
def test_intersect_halfplanes_noop_with_no_constraints():
    verts, tags = ps._intersect_halfplanes(SQUARE, [])
    assert verts == SQUARE
    assert tags == [None] * 4


def test_intersect_halfplanes_cuts_a_corner():
    # Keep x <= 0: the right half of the square is dropped.
    verts, tags = ps._intersect_halfplanes(SQUARE, [(1.0, 0.0, 0.0, "cut")])
    assert all(x <= 1e-6 for (x, _y) in verts)
    assert "cut" in tags


def test_intersect_halfplanes_empties_out_entirely():
    # Keep x <= -1000: nothing of the square survives.
    verts, _tags = ps._intersect_halfplanes(SQUARE, [(1.0, 0.0, -1000.0, "x")])
    assert verts == []


def test_power_cell_equal_weights_is_plain_voronoi_bisector():
    seeds = [(-100.0, 0.0, 1.0), (100.0, 0.0, 1.0)]
    grid = {(int(math.floor(sx / 1000.0)), int(math.floor(sy / 1000.0))): [i]
            for i, (sx, sy, _w) in enumerate(seeds)}
    # Both seeds share a grid cell in this tiny example -- rebuild the grid
    # with every seed in every bucket the lookup might probe.
    grid = {}
    for i in range(len(seeds)):
        grid.setdefault((0, 0), []).append(i)
    verts, _tags = ps._power_cell(0, seeds, SQUARE, grid, 1000.0, 0.0, 1e-6)
    # Equal weights -> the bisector is the plain perpendicular bisector
    # (x = 0) -- seed 0's cell (x=-100) should stay entirely at x <= 0.
    assert all(x <= 1e-6 for (x, _y) in verts)


def test_power_cell_widens_search_to_find_a_distant_neighbour():
    # Regression guard for the real bug `_power_cell`'s docstring describes:
    # a fixed, un-widened search radius can miss the one seed that should
    # clip a cell. Put the only other seed many grid buckets away (far
    # outside a single 3x3 ring) and confirm the cell still gets clipped at
    # the correct bisector instead of ballooning out to the far boundary.
    big = [(-5000.0, -5000.0), (5000.0, -5000.0),
          (5000.0, 5000.0), (-5000.0, 5000.0)]
    seeds = [(0.0, 0.0, 1.0), (2500.0, 0.0, 1.0)]
    bucket_size = 100.0
    grid = {}
    for idx, (sx, sy, _w) in enumerate(seeds):
        key = (int(math.floor(sx / bucket_size)), int(math.floor(sy / bucket_size)))
        grid.setdefault(key, []).append(idx)
    verts, tags = ps._power_cell(0, seeds, big, grid, bucket_size, 0.0, 1e-6)
    assert all(x <= 1250.0 + 1e-6 for (x, _y) in verts)
    assert 1 in tags


def test_dedupe_polygon_vertices_merges_a_tiny_edge():
    verts = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0),
            (100.0005, 100.0005), (0.0, 100.0)]
    tags = ["a", "b", "c", "d", "e"]
    out_v, out_t = ps._dedupe_polygon_vertices(verts, tags, 0.01)
    assert len(out_v) == 4
    assert (100.0005, 100.0005) not in out_v


def test_dedupe_polygon_vertices_leaves_a_normal_polygon_unchanged():
    out_v, out_t = ps._dedupe_polygon_vertices(SQUARE, [None] * 4, 1e-6)
    assert out_v == SQUARE


def test_dedupe_polygon_vertices_keeps_a_triangle_as_is():
    tri = [(0.0, 0.0), (10.0, 0.0), (5.0, 8.0)]
    out_v, out_t = ps._dedupe_polygon_vertices(tri, ["a", "b", "c"], 1000.0)
    assert out_v == tri   # never collapses below a triangle


def test_inset_cell_zero_gap_is_unchanged():
    tags = [None, 1, None, 1]
    verts = ps._inset_cell(SQUARE, tags, 0.0, 0, 99)
    assert verts == pytest.approx(SQUARE)


def test_inset_cell_shrinks_tagged_edges_only():
    tags = [None, 1, None, None]   # only the right edge (index 1) is mortar
    verts = ps._inset_cell(SQUARE, tags, 40.0, 0, 99)
    xs = [x for (x, _y) in verts]
    ys = [y for (_x, y) in verts]
    assert max(xs) < 400.0 - 1e-6     # right edge moved inward
    assert min(xs) == pytest.approx(-400.0)   # left (untagged) edge untouched
    assert max(ys) == pytest.approx(400.0)
    assert min(ys) == pytest.approx(-400.0)


# ---------------------------------------------------------------------------
# Flagstone mesh
# ---------------------------------------------------------------------------
_HEX_OUTLINE = [(200.0 * math.cos(2 * math.pi * i / 6),
                 200.0 * math.sin(2 * math.pi * i / 6)) for i in range(6)]


@pytest.mark.parametrize("outline", [
    _HEX_OUTLINE,
    [(-100.0, -100.0), (100.0, -100.0), (100.0, 100.0), (-100.0, 100.0)],
    [(0.0, 0.0), (120.0, 0.0), (60.0, 90.0)],
])
def test_flagstone_mesh_is_manifold(outline):
    verts, faces = ps.flagstone_mesh(outline, 60.0)
    assert_two_manifold(verts, faces)


def test_flagstone_mesh_vertex_and_face_count_matches_outline():
    verts, faces = ps.flagstone_mesh(_HEX_OUTLINE, 60.0)
    n = len(_HEX_OUTLINE)
    assert len(verts) == 2 * n
    assert len(faces) == n + 2


def test_flagstone_mesh_bottom_ring_is_flat_at_zero():
    n = len(_HEX_OUTLINE)
    verts, _ = ps.flagstone_mesh(_HEX_OUTLINE, 60.0)
    for (_x, _y, z) in verts[:n]:
        assert z == pytest.approx(0.0)


def test_flagstone_mesh_top_ring_is_flat_at_thickness():
    n = len(_HEX_OUTLINE)
    verts, _ = ps.flagstone_mesh(_HEX_OUTLINE, 60.0)
    for (_x, _y, z) in verts[n:]:
        assert z == pytest.approx(60.0)


def test_flagstone_mesh_bottom_ring_matches_outline_xy():
    n = len(_HEX_OUTLINE)
    verts, _ = ps.flagstone_mesh(_HEX_OUTLINE, 60.0)
    for (ox, oy), (x, y, _z) in zip(_HEX_OUTLINE, verts[:n]):
        assert (x, y) == pytest.approx((ox, oy))


# ---------------------------------------------------------------------------
# Assembly (tilt fitting)
# ---------------------------------------------------------------------------
def test_assembly_is_manifold():
    placements = _scatter()
    verts, faces = ps.assemble_flagstone_mesh(placements, lambda x, y: 5.0)
    assert_two_manifold(verts, faces)


def test_assembly_counts_are_sum_of_flagstones():
    placements = _scatter()
    verts, faces = ps.assemble_flagstone_mesh(placements, lambda x, y: 0.0)
    expected_v = expected_f = 0
    for (_x, _y, outline, _pid) in placements:
        pv, pf = ps.flagstone_mesh(outline, 60.0)
        expected_v += len(pv)
        expected_f += len(pf)
    assert len(verts) == expected_v
    assert len(faces) == expected_f


def test_assembly_empty_is_empty():
    verts, faces = ps.assemble_flagstone_mesh([], lambda x, y: 0.0)
    assert verts == [] and faces == []


def test_assembly_skips_degenerate_outlines():
    placements = [(0.0, 0.0, [(0.0, 0.0), (1.0, 0.0)], 1)]   # only 2 points
    verts, faces = ps.assemble_flagstone_mesh(placements, lambda x, y: 0.0)
    assert verts == [] and faces == []


def test_assembly_flat_terrain_sits_at_sampled_height_minus_sink():
    outline = [(100.0, 0.0), (-50.0, 86.6), (-50.0, -86.6)]
    placements = [(0.0, 0.0, outline, 1)]
    verts, _ = ps.assemble_flagstone_mesh(placements, lambda x, y: 20.0,
                                          sink_mm=1.0, thickness_mm=60.0)
    n = len(outline)
    bottoms = verts[:n]
    tops = verts[n:]
    for (_x, _y, z) in bottoms:
        assert z == pytest.approx(19.0, abs=1e-6)
    for (_x, _y, z) in tops:
        assert z == pytest.approx(19.0 + 60.0, abs=1e-6)


def test_assembly_tilts_to_match_a_sloped_plane():
    # The fit samples (not the lift recheck) are deliberately taken from a
    # point nudged very slightly toward the stone's centroid, not the exact
    # corner (`_shrink_toward_centroid` -- see assemble_flagstone_mesh's own
    # docstring) -- so the fitted plane carries a correspondingly tiny bias
    # versus the true sampled plane. `abs=0.1` accounts for that deliberate,
    # bounded bias (`_SAMPLE_INWARD_SHRINK` is 0.2%, so at this ~100mm
    # outline radius and slope the expected bias is a few hundredths of a
    # unit) while still catching anything actually wrong with the fit.
    slope = 0.3
    sample = lambda x, y: slope * x
    outline = [(100.0, 0.0), (-50.0, 86.6), (-50.0, -86.6)]
    placements = [(0.0, 0.0, outline, 1)]
    verts, _ = ps.assemble_flagstone_mesh(placements, sample, sink_mm=0.0,
                                          thickness_mm=60.0, max_tilt_deg=45.0)
    n = len(outline)
    bottoms = verts[:n]
    for (x, y, z) in bottoms:
        assert z == pytest.approx(sample(x, y), abs=0.1)
    zs = [z for (_x, _y, z) in bottoms]
    assert max(zs) - min(zs) > 1.0


def test_assembly_fit_ignores_a_bad_sample_exactly_at_one_corner():
    # Regression guard for a real, reported bug: a raycast-based
    # corner_sampler can return a wildly wrong height specifically at a
    # point sitting exactly on the region's own boundary (where it can
    # coincide with the hex's physical rim -- see flagstones.py's
    # `_make_corner_sampler`). The fit samples a point nudged very slightly
    # toward the stone's own centroid instead of the literal corner
    # (`_shrink_toward_centroid`), so a sampler that's only wrong AT THE
    # EXACT corner coordinate shouldn't corrupt the fit the way it would if
    # sampled exactly there -- the real-world symptom was a stone tilted
    # (and clamped to max_tilt_deg) so hard it ended up resting on a single
    # corner.
    # (The lift recheck afterward DOES still sample the exact corner by
    # design -- see assemble_flagstone_mesh's own docstring -- so it can
    # independently push the whole stone's absolute height around if it
    # also happens to land on `bad_point`; that's an artifact of this
    # synthetic single-sampler setup, not something production code relies
    # on this shrink for. What matters here, and the only thing asserted,
    # is that the FIT's own tilt isn't corrupted by it.)
    outline = [(100.0, 0.0), (-50.0, 86.6), (-50.0, -86.6)]
    placements = [(0.0, 0.0, outline, 1)]
    bad_point = (100.0, 0.0)

    def sampler(x, y):
        if (x, y) == bad_point:
            return 999.0   # catastrophically wrong, only at the exact corner
        return 20.0

    verts, _ = ps.assemble_flagstone_mesh(placements, sampler, sink_mm=0.0,
                                          thickness_mm=60.0, max_tilt_deg=25.0)
    n = len(outline)
    zs = [z for (_x, _y, z) in verts[:n]]
    assert max(zs) - min(zs) < 1.0   # relatively flat -- not tilted/clamped


def test_assembly_never_sinks_a_corner_below_sampled_terrain():
    def sample(x, y):
        return 10.0 * math.sin(x / 137.0) * math.cos(y / 211.0)

    placements = _scatter(min_size_mm=400.0, max_size_mm=400.0)
    sink_mm = 0.0
    verts, faces = ps.assemble_flagstone_mesh(placements, sample, sink_mm=sink_mm,
                                              max_tilt_deg=15.0)
    idx = 0
    for (_x, _y, outline, _pid) in placements:
        n = len(outline)
        for (x, y, z) in verts[idx:idx + n]:   # bottom-ring (contact) corners
            assert z >= sample(x, y) - 1e-6 - sink_mm
        idx += 2 * n


def test_assembly_bottom_ring_is_always_at_or_above_top_of_terrain_minus_sink():
    outline = [(100.0, 0.0), (-50.0, 86.6), (-50.0, -86.6)]
    placements = [(0.0, 0.0, outline, 1)]
    verts, _ = ps.assemble_flagstone_mesh(placements, lambda x, y: 0.0,
                                          sink_mm=0.0, thickness_mm=60.0)
    n = len(outline)
    bottom_z = [z for (_x, _y, z) in verts[:n]]
    top_z = [z for (_x, _y, z) in verts[n:]]
    assert min(top_z) > max(bottom_z)


def test_assembly_tilt_is_clamped():
    slope = 5.0   # ~78.7 degrees unclamped -- must be clamped down hard
    sample = lambda x, y: slope * x
    outline = [(100.0, 0.0), (-50.0, 86.6), (-50.0, -86.6)]
    placements = [(0.0, 0.0, outline, 1)]
    max_tilt_deg = 20.0
    verts, _ = ps.assemble_flagstone_mesh(placements, sample, sink_mm=0.0,
                                          thickness_mm=60.0,
                                          max_tilt_deg=max_tilt_deg)
    n = len(outline)
    bottoms = verts[:n]
    xs = [x for (x, _y, _z) in bottoms]
    zs = [z for (_x, _y, z) in bottoms]
    dx = max(xs) - min(xs)
    dz = max(zs) - min(zs)
    assert dx > 0.0
    observed_slope = dz / dx
    assert observed_slope <= math.tan(math.radians(max_tilt_deg)) + 1e-6


def test_assembly_corner_sampler_called_with_translated_coordinates():
    seen = []

    def sample(x, y):
        seen.append((x, y))
        return 0.0

    outline = [(100.0, 0.0), (-50.0, 86.6), (-50.0, -86.6)]
    placements = [(100.0, 200.0, outline, 1)]
    ps.assemble_flagstone_mesh(placements, sample)
    for (x, y) in seen:
        assert math.hypot(x - 100.0, y - 200.0) < 150.0


# ---------------------------------------------------------------------------
# Rotation helper
# ---------------------------------------------------------------------------
def test_rotate_to_normal_identity_for_up():
    mat = ps._rotate_to_normal(0.0, 0.0, 1.0)
    assert ps._apply_rotation(mat, (3.0, -2.0, 5.0)) == pytest.approx((3.0, -2.0, 5.0))


def test_rotate_to_normal_maps_up_to_normal():
    for normal in [(0.3, 0.1, 0.9), (-0.2, 0.4, 0.88), (0.0, 0.0, 1.0)]:
        n = ps._normalize(normal)
        mat = ps._rotate_to_normal(*n)
        result = ps._apply_rotation(mat, (0.0, 0.0, 1.0))
        assert result == pytest.approx(n, abs=1e-9)


def test_rotate_to_normal_preserves_length():
    mat = ps._rotate_to_normal(*ps._normalize((0.4, -0.3, 0.9)))
    v = (2.0, -1.0, 3.0)
    r = ps._apply_rotation(mat, v)
    len_before = math.sqrt(sum(c * c for c in v))
    len_after = math.sqrt(sum(c * c for c in r))
    assert len_after == pytest.approx(len_before)


# ---------------------------------------------------------------------------
# estimate_flagstone_count
# ---------------------------------------------------------------------------
def test_estimate_flagstone_count_tracks_actual_order_of_magnitude():
    big_square = [(-2000.0, -2000.0), (2000.0, -2000.0),
                 (2000.0, 2000.0), (-2000.0, 2000.0)]
    kw = dict(min_size_mm=300.0, max_size_mm=500.0, gap_mm=10.0)
    est = ps.estimate_flagstone_count(big_square, **kw)
    actual = len(ps.scatter_flagstones(big_square, seed=3, **kw))
    assert est > 0
    assert 0.4 * est <= actual <= 1.6 * est


def test_estimate_flagstone_count_degenerate_is_zero():
    assert ps.estimate_flagstone_count([(0.0, 0.0)], min_size_mm=10.0,
                                       max_size_mm=20.0, gap_mm=5.0) == 0


def test_estimate_flagstone_count_zero_max_size_is_zero():
    assert ps.estimate_flagstone_count(SQUARE, min_size_mm=0.0,
                                       max_size_mm=0.0, gap_mm=5.0) == 0


# ---------------------------------------------------------------------------
# Registry — FLAGSTONE integrates as a scatter surface without touching the
# displace contract (same assertions test_scatter.py makes for BOULDERS).
# ---------------------------------------------------------------------------
def test_flagstone_is_a_scatter_surface():
    assert ps.surface_kind("FLAGSTONE") == 'scatter'
    assert ps.SURFACES["FLAGSTONE"].generator is None
    assert ps.SURFACES["FLAGSTONE"].placement_fn is ps.scatter_flagstones
    assert ps.SURFACES["FLAGSTONE"].element_mesh_fn is ps.flagstone_mesh


def test_flagstone_excluded_from_generating_fanout():
    generating = [k for k, s in ps.SURFACES.items() if s.generator is not None]
    assert "FLAGSTONE" not in generating


def test_flagstone_contributes_zero_displacement():
    assert ps.surface_offset(3.0, 4.0, surface_type="FLAGSTONE", feature_mm=10.0,
                             depth_mm=5.0, regularity=0.5, seed=1) == 0.0


def test_flagstone_extra_param_defaults_keys_and_values():
    d = ps.extra_param_defaults("FLAGSTONE", 1800.0)
    assert set(d) == {"min_size_mm", "max_size_mm", "gap_mm", "thickness_mm"}
    assert d["min_size_mm"] == pytest.approx(420.0)
    assert d["max_size_mm"] == pytest.approx(650.0)
    assert d["gap_mm"] == pytest.approx(10.0)
    assert d["thickness_mm"] == pytest.approx(60.0)


def test_flagstone_extra_param_mm_defaults_scale_with_man_height():
    d = ps.extra_param_defaults("FLAGSTONE", 10.0)
    assert d["min_size_mm"] == pytest.approx(420.0 * 10.0 / 1800.0)
    assert d["max_size_mm"] == pytest.approx(650.0 * 10.0 / 1800.0)


def test_flagstone_gap_param_label_is_average():
    spec = next(p for p in ps.SURFACES["FLAGSTONE"].extra_params if p.key == "gap_mm")
    assert spec.label == "Avg. Mortar Gap (mm)"


def test_both_scatter_surfaces_are_excluded_from_generating_fanout():
    generating = [k for k, s in ps.SURFACES.items() if s.generator is not None]
    assert "BOULDERS" not in generating
    assert "FLAGSTONE" not in generating


# ---------------------------------------------------------------------------
# segment_geometry reuse sanity — the exact fit this module relies on.
# ---------------------------------------------------------------------------
def test_fit_resting_plane_is_available_and_used():
    # Not a re-test of segment_geometry's own suite -- just confirms the
    # import this module depends on resolves to the real function.
    assert sg.fit_resting_plane([(0, 0, 1), (1, 0, 1), (0, 1, 1)]) is not None


# ---------------------------------------------------------------------------
# Interior-aware lift: terrain between a stone's corners must never poke up
# through the slab (the "flagstones sink into the surface" bug).

def _stone_bottoms(placements, verts):
    i = 0
    for (_x, _y, outline, _pid) in placements:
        n = len(outline)
        yield verts[i:i + n]
        i += 2 * n


def _max_interior_penetration(placements, verts, sampler):
    worst = -float("inf")
    for ring in _stone_bottoms(placements, verts):
        n = len(ring)
        cx = sum(p[0] for p in ring) / n
        cy = sum(p[1] for p in ring) / n
        cz = sum(p[2] for p in ring) / n
        for p in ring:
            for t in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0):
                px, py = cx + (p[0] - cx) * t, cy + (p[1] - cy) * t
                pz = cz + (p[2] - cz) * t
                worst = max(worst, sampler(px, py) - pz)
    return worst


@pytest.mark.parametrize("sampler", [
    lambda x, y: 15.0 - 0.01 * x * x,                                  # hill
    lambda x, y: 15.0 - 0.01 * x * x
    + 0.3 * math.sin(x * 2.1) * math.cos(y * 1.7),                     # bumpy hill
    lambda x, y: 10.0 + 0.4 * math.exp(-(x * x + y * y)),              # centre bump
])
def test_assembly_interior_never_penetrated_by_convex_terrain(sampler):
    poly = [(-20.0, -4.0), (20.0, -4.0), (20.0, 4.0), (-20.0, 4.0)]
    placements = ps.scatter_flagstones(poly, min_size_mm=2.33, max_size_mm=3.6,
                                       gap_mm=0.055, seed=1)
    sink = 0.05
    verts, faces = ps.assemble_flagstone_mesh(
        placements, sampler, thickness_mm=0.33, sink_mm=sink, max_tilt_deg=25.0)
    assert_two_manifold(verts, faces)
    # Sampled at a denser pattern than the lift itself uses; allow a small
    # resolution slack beyond the intended sink overlap.
    assert _max_interior_penetration(placements, verts, sampler) <= sink + 0.05


def test_bottom_face_samples_lie_on_tilted_plane():
    ring = [(0.0, 0.0, 1.0), (4.0, 0.0, 3.0), (4.0, 3.0, 4.5), (0.0, 3.0, 2.5)]
    plane = lambda x, y: 1.0 + 0.5 * x + 0.5 * y
    samples = ps._bottom_face_samples(ring)
    assert len(samples) == 2 * len(ring) + 1 + 2 * len(ps._INTERIOR_SAMPLE_RINGS) * len(ring)
    for (x, y, z) in samples:
        assert z == pytest.approx(plane(x, y), abs=1e-9)
        assert 0.0 <= x <= 4.0 and 0.0 <= y <= 3.0


def test_flagstone_thickness_min_allows_small_scale_default():
    spec = {s.key: s for s in ps.SURFACES["FLAGSTONE"].extra_params}["thickness_mm"]
    assert spec.scaled_default(10.0) >= spec.min
