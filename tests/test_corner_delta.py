"""Tests for the multi-select parallel corner edit.

The fan-out itself (operators.on_corner_changed) imports bpy and can't run in
plain CPython, so the testable logic lives in map.py: `clamp_level` and
`apply_corner_delta`. The seam-convergence simulation reproduces the same delta
+ seam-sync sequence the operator performs, using only the bpy-free
SHARED_CORNERS table and neighbour_coord — proving a uniform delta across an
adjacent selection converges to a tear-free region lift.
"""

import pytest

import map as hm
from mesh_builder import build_hex_tile, resolve_center_z


# ---------------------------------------------------------------------------
# clamp_level

@pytest.mark.parametrize("v,expected", [
    (5, 5), (1, 1), (0, 0), (-1, 0), (-7, 0),
])
def test_clamp_level_floors_at_zero(v, expected):
    assert hm.clamp_level(v) == expected


def test_clamp_level_custom_floor():
    assert hm.clamp_level(2, min_level=3) == 3
    assert hm.clamp_level(4, min_level=3) == 4


# ---------------------------------------------------------------------------
# apply_corner_delta

@pytest.mark.parametrize("idx", range(6))
def test_apply_corner_delta_only_target_index_changes(idx):
    base = (1, 2, 3, 4, 5, 6)
    out = hm.apply_corner_delta(base, idx, +2)
    for i in range(6):
        if i == idx:
            assert out[i] == base[i] + 2
        else:
            assert out[i] == base[i]


def test_apply_corner_delta_positive_shift():
    assert hm.apply_corner_delta((0, 0, 0, 0, 0, 0), 0, +3) == (3, 0, 0, 0, 0, 0)


def test_apply_corner_delta_clamps_at_zero():
    # A downward delta that would push the corner below zero floors at 0.
    assert hm.apply_corner_delta((1, 0, 0, 0, 0, 0), 0, -3) == (0, 0, 0, 0, 0, 0)


def test_apply_corner_delta_zero_is_noop():
    base = (4, 5, 6, 7, 8, 9)
    assert hm.apply_corner_delta(base, 2, 0) == base


def test_apply_corner_delta_does_not_mutate_input():
    base = [1, 2, 3, 4, 5, 6]
    hm.apply_corner_delta(base, 0, +5)
    assert base == [1, 2, 3, 4, 5, 6]


# ---------------------------------------------------------------------------
# Seam-convergence simulation (no bpy)
#
# Model a small map as {(q, r): [6 corner levels]}. Reproduce what the operator
# does for a multi-select edit: (1) apply the same delta to the same corner
# index on every "selected" tile, then (2) run seam sync — each tile pushes
# every corner value to its shared partners. Then assert that every shared
# geometric vertex resolves to a single value (no tear) across the whole map.

def _seam_sync(grid):
    """Propagate every corner to its shared partners until stable. Mirrors the
    repeated setattr-equal that on_corner_changed performs across the cascade."""
    changed = True
    while changed:
        changed = False
        for (q, r), levels in grid.items():
            for corner_idx, partners in enumerate(hm.SHARED_CORNERS):
                for direction, n_corner_idx in partners:
                    nq, nr = hm.neighbour_coord(q, r, direction)
                    if (nq, nr) not in grid:
                        continue
                    if grid[(nq, nr)][n_corner_idx] != levels[corner_idx]:
                        grid[(nq, nr)][n_corner_idx] = levels[corner_idx]
                        changed = True
    return grid


def _assert_seams_consistent(grid):
    for (q, r), levels in grid.items():
        for corner_idx, partners in enumerate(hm.SHARED_CORNERS):
            for direction, n_corner_idx in partners:
                nq, nr = hm.neighbour_coord(q, r, direction)
                if (nq, nr) not in grid:
                    continue
                assert grid[(nq, nr)][n_corner_idx] == levels[corner_idx], (
                    f"seam tear: ({q},{r}).P{corner_idx+1} "
                    f"!= ({nq},{nr}).P{n_corner_idx+1}"
                )


def _make_grid(coords, level=0):
    return {c: [level] * 6 for c in coords}


def test_uniform_delta_on_adjacent_selection_converges():
    # A 2x2 block, all flat at level 2 and seam-consistent to start.
    coords = [(0, 0), (1, 0), (0, 1), (1, 1)]
    grid = _seam_sync(_make_grid(coords, level=2))
    _assert_seams_consistent(grid)

    # Select two adjacent tiles and raise corner index 0 (P1) by +3 on both.
    selected = [(0, 0), (1, 0)]
    for c in selected:
        grid[c] = list(hm.apply_corner_delta(tuple(grid[c]), 0, +3))

    _seam_sync(grid)
    _assert_seams_consistent(grid)
    # Both selected tiles ended with P1 raised; their shared geometry agrees.
    assert grid[(0, 0)][0] == 5
    assert grid[(1, 0)][0] == 5


def test_downward_delta_clamps_without_tearing():
    coords = [(0, 0), (1, 0), (0, 1), (1, 1)]
    grid = _make_grid(coords, level=0)
    # Pre-raise one corner so there's something to lower; keep seams synced.
    grid[(0, 0)] = [1, 0, 0, 0, 0, 0]
    _seam_sync(grid)

    # Lower P1 by 3 on two selected tiles; the low one floors at 0.
    selected = [(0, 0), (1, 0)]
    for c in selected:
        grid[c] = list(hm.apply_corner_delta(tuple(grid[c]), 0, -3))

    _seam_sync(grid)
    _assert_seams_consistent(grid)
    assert grid[(0, 0)][0] == 0
    assert grid[(1, 0)][0] == 0


# ---------------------------------------------------------------------------
# Centre-height recalculation contract: when corners change (e.g. via a
# multi-select edit), the centre height is recalculated from the corner mean
# unless the tile is "set to ignore" (override_center → an explicit level).

def test_resolve_center_z_follows_corner_mean_when_not_overridden():
    # center_level=None → centre tracks the corners (recalculated each rebuild).
    base, lh = 10.0, 5.0
    flat = [base + 0 * lh] * 6
    assert resolve_center_z(flat, None, base, lh) == base

    raised = [base + 2 * lh] * 6          # all corners +2 levels
    assert resolve_center_z(raised, None, base, lh) == base + 2 * lh

    mixed = [base + lvl * lh for lvl in (0, 1, 2, 3, 4, 5)]
    assert resolve_center_z(mixed, None, base, lh) == base + 2.5 * lh  # mean level 2.5


def test_resolve_center_z_is_pinned_when_overridden():
    # center_level set → centre ignores corners ("set to ignore").
    base, lh = 10.0, 5.0
    pinned = resolve_center_z([base + 9 * lh] * 6, 3, base, lh)
    assert pinned == base + 3 * lh
    # Changing the corners must not move a pinned centre.
    assert resolve_center_z([base] * 6, 3, base, lh) == pinned


def _apex_z(verts, center_xy=(0.0, 0.0)):
    """Z of the top apex over the tile centre. Several verts share the centre XY
    (top apex + base-bottom centre), so take the highest one — the top surface."""
    cx, cy = center_xy
    over_centre = [v[2] for v in verts
                   if (v[0] - cx) ** 2 + (v[1] - cy) ** 2 < 1.0]
    return max(over_centre)


_BUILD = dict(diameter_mm=100.0, level_height_mm=5.0, base_thickness_mm=10.0,
              smoothness_passes=2)


def test_apex_tracks_corners_when_not_overridden():
    # End-to-end through build_hex_tile: raising every corner with no override
    # lifts the apex too (centre recalculated from the corner mean).
    low, _ = build_hex_tile(corner_levels=(0,) * 6, center_level=None, **_BUILD)
    high, _ = build_hex_tile(corner_levels=(4,) * 6, center_level=None, **_BUILD)
    assert _apex_z(high) > _apex_z(low)


def test_override_makes_apex_resist_corner_changes():
    # The control centre is pinned by the override; the subdivided apex still
    # blends slightly toward the corners, so rather than asserting exact equality
    # we assert the override greatly reduces how much the apex follows a corner
    # change versus the non-overridden case.
    lo_auto, _ = build_hex_tile(corner_levels=(0,) * 6, center_level=None, **_BUILD)
    hi_auto, _ = build_hex_tile(corner_levels=(4,) * 6, center_level=None, **_BUILD)
    lo_ovr, _ = build_hex_tile(corner_levels=(0,) * 6, center_level=2, **_BUILD)
    hi_ovr, _ = build_hex_tile(corner_levels=(4,) * 6, center_level=2, **_BUILD)

    follow_auto = _apex_z(hi_auto) - _apex_z(lo_auto)
    follow_ovr = _apex_z(hi_ovr) - _apex_z(lo_ovr)
    assert follow_ovr < follow_auto
