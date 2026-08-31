"""Grid-clustering math for terrain-object plateau pads.

No `bpy` imports — same constraint as `mesh_builder.py` / `tree_pads.py` so
this module is unit-testable in plain CPython.

A terrain object's footprint is arbitrary (unlike a planted tree's circular
base cut), so it can't be covered by a single `tree_pads.refine_and_flatten`
pad. Instead, the caller (`operators.terrain_pad_specs`) samples a grid of
points across the model's bbox, classifies each as "under the model's flat
base" or not via an up-raycast (`operators._raycast_under_flat_base`), and
hands the resulting hit/miss grid to `cluster_grid_hits` here, which turns
contiguous hit cells into a small set of circular pads — one or a few per
solid region, none covering a hole or a gap between disjoint regions.
"""

import math


def cluster_grid_hits(hits, spacing_mm, margin=1.25, block_cells=3):
    """Tile "hit" grid cells into small circular pad specs.

    `hits` is an iterable of `(col, row, x, y)` tuples — one per grid cell
    that was classified as under the model's flat base (the caller has
    already done the inside/outside classification; only hit cells are
    passed in). `col`/`row` are integer grid indices; `x`/`y` are that
    cell's tile-local mm position.

    Cells are grouped into `block_cells x block_cells` blocks by grid index
    (`col // block_cells, row // block_cells`), and each non-empty block
    becomes one `{"x", "y", "radius_mm"}` pad, sized only from that block's
    own member cells: `x`/`y` are the member centroid, `radius_mm` is the
    max centroid-to-cell distance, padded by half a grid spacing (so the
    circle covers each member cell's own footprint, not just its sample
    point) and inflated by `margin`.

    This is deliberately block-tiling rather than one circle per connected
    component: a footprint that wraps around a hole (e.g. a ring) is still
    one 4-connected region, but its centroid — and therefore a single
    circle's full radius — can land squarely on the hole. Tiling into
    blocks bounds any one pad's span to roughly `block_cells` grid cells,
    so it can bridge at most a hole/gap of about that size, the same kind
    of grid-resolution limit the raycast sampling and notch-drilling code
    already live with elsewhere in this codebase. Two disjoint regions of
    the footprint, or a hole wider than a block, naturally end up as
    separate pads with no pad spanning between them, since a block with no
    hits emits no pad.

    Returns a list of pad dicts, in no particular order.
    """
    blocks = {}
    for col, row, x, y in hits:
        key = (col // block_cells, row // block_cells)
        blocks.setdefault(key, []).append((x, y))

    pads = []
    for members in blocks.values():
        cx = sum(x for x, _y in members) / len(members)
        cy = sum(y for _x, y in members) / len(members)
        max_dist = max(math.hypot(x - cx, y - cy) for x, y in members)
        radius_mm = (max_dist + spacing_mm * 0.5) * margin
        pads.append({"x": cx, "y": cy, "radius_mm": radius_mm})

    return pads
