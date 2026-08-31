import math

from terrain_pads import cluster_grid_hits


def _grid(cols, rows, spacing, is_hit):
    """Build a (col, row, x, y) hit list over a cols x rows grid, spacing_mm
    apart, keeping only cells where is_hit(col, row) is True."""
    hits = []
    for row in range(rows):
        for col in range(cols):
            if is_hit(col, row):
                hits.append((col, row, col * spacing, row * spacing))
    return hits


def test_empty_hits_produce_no_pads():
    assert cluster_grid_hits([], spacing_mm=5.0) == []


def test_small_solid_blob_within_one_block_becomes_one_pad():
    # Entirely inside a single block, so it can't be split.
    hits = _grid(3, 3, 5.0, lambda c, r: True)
    pads = cluster_grid_hits(hits, spacing_mm=5.0, margin=1.0, block_cells=3)
    assert len(pads) == 1
    pad = pads[0]
    assert pad["x"] == 5.0
    assert pad["y"] == 5.0
    assert pad["radius_mm"] > 0.0


def test_large_solid_blob_splits_across_multiple_blocks():
    hits = _grid(6, 6, 5.0, lambda c, r: True)
    pads = cluster_grid_hits(hits, spacing_mm=5.0, margin=1.0, block_cells=3)
    # 6x6 cells / 3x3 blocks -> a 2x2 grid of blocks.
    assert len(pads) == 4


def test_hole_is_never_covered_by_a_single_pad():
    # A ring of hit cells around one missing center cell — still one
    # 4-connected region. A single bounding-circle-per-component approach
    # would center its one pad exactly on the hole (dist == 0, radius
    # spanning the whole ring); block-tiling must split it into several
    # smaller pads instead, none centered on the hole.
    hits = _grid(5, 5, 5.0, lambda c, r: not (c == 2 and r == 2))
    pads = cluster_grid_hits(hits, spacing_mm=5.0, margin=1.0, block_cells=2)
    assert len(pads) > 1
    hole_x, hole_y = 2 * 5.0, 2 * 5.0
    for pad in pads:
        dist_to_hole = math.hypot(pad["x"] - hole_x, pad["y"] - hole_y)
        assert dist_to_hole > 0.0


def test_two_disjoint_blobs_produce_separate_pads_with_no_pad_between():
    hits = _grid(2, 2, 5.0, lambda c, r: True)
    far_offset_col = 21  # block-aligned (block_cells=3) so this blob stays one block
    hits += [
        (far_offset_col + c, r, (far_offset_col + c) * 5.0, r * 5.0)
        for r in range(2) for c in range(2)
    ]
    pads = cluster_grid_hits(hits, spacing_mm=5.0, margin=1.0, block_cells=3)
    assert len(pads) == 2
    xs = sorted(p["x"] for p in pads)
    assert xs[1] - xs[0] > 50.0


def test_margin_inflates_radius():
    hits = _grid(3, 3, 5.0, lambda c, r: True)
    small = cluster_grid_hits(hits, spacing_mm=5.0, margin=1.0, block_cells=3)[0]
    big = cluster_grid_hits(hits, spacing_mm=5.0, margin=2.0, block_cells=3)[0]
    assert big["radius_mm"] == small["radius_mm"] * 2.0


def test_single_cell_gets_a_nonzero_radius_covering_the_cell():
    hits = [(0, 0, 0.0, 0.0)]
    pads = cluster_grid_hits(hits, spacing_mm=6.0, margin=1.0)
    assert len(pads) == 1
    assert pads[0]["radius_mm"] == 3.0  # half the grid spacing
