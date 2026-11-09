# Flagstone Paving — a tilt-fitted scatter surface

**Flagstone Paving** is the second member of the `scatter` surface kind (see
[Boulder Field](boulder-field.md) for the first). Where **Cobblestone**
(a `displace` surface) fakes stone shapes by bending the tile's existing
heightfield — bounded by the mesh's own vertex spacing, so it reads as smooth
bumps rather than real flat-topped slabs — Flagstone Paving places genuinely
**distinct, discrete paving-slab objects** on top of the tile and leaves the
tile surface untouched, the same mechanism Boulder Field already uses.

This page covers the flagstone algorithm (in particular the per-stone tilt
fit, which Boulder Field doesn't need), the scene tree, merging for print, and
the manual checklist for the bpy-only parts. The shared region authoring UX
and the displacement kind are in
[procedural_surfaces.md](procedural_surfaces.md); the scatter kind in general
is in [boulder-field.md](boulder-field.md); the geometry baseline is in the
main [README](../README.md).

## Why flagstones need more than a boulder's seating

A boulder is a rough, freely-tumbled rock — seating it at a single sampled
height under its centre and spinning it around Z looks right regardless of
the local slope. A flagstone is a flat paving slab: on a sloped or domed hex,
seating it flat at one height would leave it visibly floating above the
terrain on one side and dug into it on the other. Each flagstone is instead
**individually tilted** to rest flush against the local terrain, the same way
a real paver settles onto uneven ground.

## The pure / impure split (the bpy-free invariant)

All geometry math is bpy-free, unit-tested (`tests/test_flagstones.py`), and
manifold-checked, exactly like Boulder Field. Only object instantiation, the
per-corner seating raycasts, and the boolean merge touch `bpy`.

### bpy-free — `procedural_surfaces.py`

1. **Placement** — `scatter_flagstones(polygon, *, min_size_mm, max_size_mm,
   gap_mm, seed) → [(x, y, outline_local, pid)]` in tile-local XY (Z/tilt
   resolved later). This is a **power diagram (weighted Voronoi) tessellation**
   of the region, not a packing algorithm: seed the region with a
   hex-staggered grid (pitch = the mean of min/max size) plus the centroid,
   each seed jittered in position and given its own random target
   size/weight; compute each seed's cell by clipping the region polygon
   against the "closer to me than you" halfplane of every nearby seed
   (`_power_cell`, a weighted Voronoi/Laguerre bisector — bigger weight
   claims more area, which is what makes Min/Max Stone Size actually shape
   the result); then **inset** each cell along its shared seams
   (`_inset_cell`) by an independently-jittered half of `gap_mm` on each
   side (mean `gap_mm/2`), so two neighbouring cells' insets sum to `gap_mm`
   *on average* — some seams come out narrower, some wider, and once an
   inset clamps against a small cell the two stones simply touch (gap 0).
   A tessellation covers the whole region by construction, so this reaches
   near-complete coverage with no leftover holes — the earlier
   circle-packing algorithm's actual failure mode (uncovered pockets between
   separately-grown fronts), which read as "the gap is much wider than the
   parameter" even though its own pairwise minimum-gap invariant held. Both
   steps reduce to the same primitive, `_intersect_halfplanes` (a tagged
   Sutherland-Hodgman clip — tags track which edges are the region's own rim
   vs. a mortar seam with a specific neighbour, so only the latter get
   inset). `_power_cell`'s own neighbour search widens itself as needed
   (see its docstring) rather than trusting a single fixed-radius ring — a
   real bug this fixes: random seed jitter can leave a locally sparser patch
   somewhere, and a fixed search radius could miss the one seed that should
   have clipped a cell in that direction, letting it balloon out toward the
   region's far boundary instead (one hugely oversized stone floating above
   an otherwise-correct tessellation). `_dedupe_polygon_vertices` is a
   second small cleanup both `_power_cell` and the final inset result run
   through: sequential half-plane clipping can leave a near-duplicate vertex
   behind (two different clip lines crossing the boundary at nearly the same
   point), which would otherwise survive as a razor-thin sliver edge in the
   final mesh — most visible on cells that touch the region's own rim, since
   that's where a seed's cell is most likely to be clipped by both a
   boundary edge and a neighbour's bisector in close succession. A third,
   related check: a seed landing right next to a tight/acute corner of the
   region's own boundary (a hand-drawn or flood-filled region's boundary can
   have one; the hex's own 6 corners never do) can get clipped into a
   WHOLE-CELL sliver — far smaller than its own drawn size, not just one
   short edge — which is geometrically a valid tessellation cell but not a
   usable paving stone, and (being so small) disproportionately sensitive to
   any local height variation, so it tilts hard against whatever the terrain
   happens to do right at that one corner: a single oddly-angled sliver
   standing out among otherwise normal stones. Any placement whose final
   area falls below `_FLAGSTONE_MIN_CELL_AREA_FRACTION` (20%) of its own
   seed's nominal circular area (`pi * weight`) is dropped entirely —
   leaving a small bare patch there rather than an unusable chip. Deterministic
   from `seed` — every random draw is a hash of fixed integer coordinates,
   not Python's `random` module.
2. **Flagstone mesh** — `flagstone_mesh(outline_local, thickness_mm) →
   (verts, faces)`: a flat-topped prism built directly from the stone's own
   `outline_local` (its inset tessellation cell) — no generated n-gon, no
   per-vertex jitter, and no fixed vertex/face count across stones (`2*n`/
   `n+2` for an n-vertex outline). The irregularity of a real paving slab now
   comes from the tessellation itself (every cell is a different shape)
   rather than a synthetic wobble.
3. **Assembly (the tilt fit)** — `assemble_flagstone_mesh(placements,
   corner_sampler, *, thickness_mm, sink_mm, max_tilt_deg) → (verts, faces)`.
   For each placement:
   - Sample the real surface Z under every one of its BOTTOM-ring corners —
     the contact face, z=0 in the stone's own local frame — via the injected
     `corner_sampler(x, y)` callback (mirrors Boulder Field's injected
     `z_of`, just called once per corner instead of once per element). No
     separate spin step any more — the outline is already a unique, oriented
     cell.
   - Fit the lowest never-penetrating plane through those samples —
     `segment_geometry.fit_resting_plane`, the exact same fit
     `segment_path.py` uses to tilt a placed path segment onto its hex —
     falling back to its least-squares `fit_tilt_plane` only on the rare
     fully-degenerate input the resting fit can't handle.
   - Clamp the tilt (`segment_geometry.clamp_tilt_slopes(…, max_tilt_deg)`) —
     a safety valve against one bad/missed sample, same role it plays for
     segments.
   - Turn the plane's normal into a rigid rotation about the fitted pivot
     (`_rotate_to_normal` — a pure-Python, bpy-free analogue of
     `mathutils.Vector.rotation_difference`) and rebuild the stone's world
     vertices from it.
   - **Re-check each corner's real final position.** `fit_resting_plane` only
     guarantees "no sinking" against its own *linear* plane model evaluated
     at each corner's original (pre-rotation) flat position — on genuinely
     non-planar terrain, a rotated corner's true final (x, y) drifts
     slightly off that flat position (a rotation, unlike a plane, doesn't
     preserve X/Y), so the real terrain sampled there can differ from what
     the fit saw. Re-sampling `corner_sampler` at each corner's actual final
     (x, y) and lifting the whole rigid stone straight up by whatever small
     amount clears the worst case restores the "never penetrates" guarantee
     against the *true* terrain — the identical corner re-check
     `segment_path.py`'s own placement fit performs, for the identical
     reason.
   - Apply the `sink_mm` overlap (so a later boolean merge stays manifold,
     same as Boulder Field) — unlike Boulder Field's fixed `SINK_MM`,
     `flagstones.py` scales this as `min(SINK_MM_MAX, thickness_mm *
     SINK_FRACTION)`: **Man Height** defaults to 10mm and is routinely set
     well below life-size, which scales a thin slab's `thickness_mm` down
     with it, so a flat ~1mm sink would otherwise exceed (or swallow
     entirely) the slab's own thickness — burying the whole stone below the
     surface instead of just embedding its bottom.

   The stones share no vertices, so the concatenation of closed 2-manifold
   prisms is itself 2-manifold.

`estimate_flagstone_count(polygon, …)` backs the panel's vertex-budget
warning, mirroring `estimate_boulder_count`'s shape.

### bpy shell — `flagstones.py`

`sync_flagstones(tile_obj, region_idx, region_dict, name)` mirrors
`scatter.sync_scatter` exactly (object lifecycle, tags, naming, manifold
check), except its raycast sampler (`_make_corner_sampler`) is called once
per flagstone *corner* rather than once per element, feeding
`assemble_flagstone_mesh`'s tilt fit. A straight-down raycast can genuinely
miss at a point that's clearly still on the tile: a Flood Fill region's
boundary is walked directly off the mesh's own rim vertices, so a
boundary-touching flagstone corner (common — mortar insets never apply to
the paved area's own rim) often lands exactly on a real mesh vertex or edge
there, a classic degenerate case for a ray-triangle intersection test (the
ray ends up coplanar with/parallel to the near-vertical side wall, or
exactly on a vertex several faces share) — this reads as "a few flagstones
along the Flood Fill boundary are angled or floating above the surface",
hitting stones along a straight run of edge rather than at the hex's own
(far less subdivided) corners, where a boundary-touching flagstone has many
fewer such exact-vertex corners to begin with. On a miss, `_make_corner_sampler`
retries at a small ring of offsets around the point (escalating through a
few small radii) before falling back to the fixed `fallback_z` — cheap,
since a miss itself is rare and each retry is still one fast BVH raycast,
and reliable regardless of which way "inward" happens to be, since some
direction in the ring always lands back inside a real face. (Two earlier
attempts were tried and reverted: a closure-captured mutable "last
successful hit" as the miss fallback, which silently carried a stale,
unrelated height across every stone in the region once any single raycast
missed; and a barycentric-interpolation replacement with no raycast-miss
case at all, whose unaccelerated per-call scan over every top triangle made
a large/Flood-Filled region's full placement pass pathologically slow
enough to lock up Blender.) `max_tilt_deg` is a module constant
(mirroring how `scatter.py` hardcodes `ROUGHNESS`/`SUBDIV`) — the four
user-facing param slots go to stone size/gap/thickness instead.
`AVG_OUTLINE_VERTS_ESTIMATE` is a rough per-stone vertex count used only by
`panel.py`'s vertex-budget warning, since there's no longer a fixed `sides`
to read.

The tile rebuild **purges all `hf_flagstone_*` children first**, then
recreates from the current scatter regions — robust to region add / remove /
reorder, same as Boulder Field. This runs inside `operators._REBUILDING`
(so the object churn never recurses) and *after* the new tile mesh is
assigned and the depsgraph re-evaluated (so every corner raycast samples the
current surface, never stale geometry). `operators.rebuild_tile` purges
*both* `scatter.py`'s and `flagstones.py`'s objects on every rebuild
regardless of which scatter surface a region currently uses, then dispatches
each region to the right module by its `surface_type`.

`HEXFINITY_OT_merge_flagstones` (Merge button): boolean-unions the flagstone
object(s) into the tile mesh with an EXACT solver, removes the loose
object(s), and re-checks 2-manifoldness — identical shape to
`HEXFINITY_OT_merge_scatter`.

## Scene tree

```
HexFinity Map (collection)
└─ HexTile_00_00            (the tile object)
   └─ Flagstones_Area 1     (joined flagstone mesh, parented under the tile)
```

Flagstone objects are ordinary mesh children, so the **STL export picks them
up automatically** as part of the tile (loose, unless you Merge first) — same
as boulders; `operators._terrain_objects`/`_is_terrain_object` also exclude
them from the terrain-plateau subsystem, same as boulders and placed path
segments.

## Parameters

| Param | Meaning |
|---|---|
| **Area Name** | Editable label; names the object `Flagstones_<name>`. Auto-defaults to `Area N`. |
| **Min / Max Stone Size (mm)** | Diameter range, used as each seed's tessellation weight (bigger -> bigger cell). mm params, so their defaults scale with **Man Height** (nominal ≈ 0.3x Man Height). |
| **Avg. Mortar Gap (mm)** | Average gap maintained between neighbouring flagstones — actual gaps vary stone-to-stone, and stones may touch (gap 0) in places. |
| **Slab Thickness (mm)** | Thickness of each flagstone slab. |
| **Merge into Tile** | Per-region preference for the Merge button (printability). |

> **Vertex budget.** A large region of small stones can still yield a heavy
> joined mesh. The panel estimates the flagstone/vertex count live and warns
> past ~200k verts — raise the sizes/gap or draw a smaller region.

## Tests (bpy-free)

`tests/test_flagstones.py` covers: placement determinism, centres-inside-
polygon, no-two-outlines-overlap, the zero-gap tessellation exactly covering
the polygon's area and leaving stones touching, average realized gap close
to the target over many neighbouring pairs, touching still reachable at a
small average gap, near-full coverage, filling a large region without holes;
the `_intersect_halfplanes`/`_power_cell`/`_inset_cell`/
`_dedupe_polygon_vertices` primitives directly (no-op/cut/empty clipping,
equal-weight bisector, a distant neighbour found via the widening search,
zero-gap no-op inset, tagged-edges-only shrink, tiny-edge merging); no
oversized outlier cells, sliver edges, or whole-cell slivers near an acute
boundary corner over many seeds, a large region, and an adversarial wedge
shape; flagstone mesh manifoldness across outline shapes,
vertex/face count matching the outline, bottom/top ring flatness; assembly
manifoldness, summed counts, degenerate-outline skip, flat-terrain seating,
tilting to match a sloped plane, never sinking a corner below sampled
(including non-planar) terrain, tilt clamping; the `_rotate_to_normal` helper
(identity for +Z, maps +Z to an arbitrary normal, length-preserving); and the
registry contract (scatter excluded from the displacement fan-out, zero
displacement, extra-param defaults and mm scaling, the gap param's "Avg."
label). Run:

```
"C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pytest tests -v
```

## Manual checklist (the bpy-only parts)

The object lifecycle, raycast seating/tilting, and boolean merge can't be
unit-tested without Blender. Verify by hand after changes:

1. **Create** — generate a single tile, select it, add a region, set surface
   = *Flagstone Paving*. A `Flagstones_<Area Name>` object appears parented
   under the tile, populated with paving slabs.
2. **Tilt** — raise/lower a corner so the tile is noticeably sloped, then
   regenerate: flagstones near the slope visibly tilt to follow it rather
   than floating or digging in; a flat tile's stones all sit flush and
   level.
3. **Determinism** — a pure height edit regenerates the same number of
   stones in the same places.
4. **Flood Fill edge stones** — Flood Fill the top (or a sub-area of it),
   add a Flagstone Paving region, and look closely at every stone whose
   footprint touches the Flood Fill boundary, especially where that
   boundary coincides with the hex's own physical rim: every one must sit
   flush and level (or correctly tilted to match real sloped/domed terrain
   there), never visibly floating above or dug at an unexplained angle.
   This is the regression check for a real, previously-shipped bug (a
   raycast-based sampler that could silently reuse a stale, unrelated
   height on a miss at the rim's knife-edge).
5. **Gap** — set a small Avg. Mortar Gap and a wide Min/Max Stone Size
   spread: the joints visibly vary in width stone-to-stone, some stones
   touch directly, and there are no empty holes in the paved area; raising
   the gap visibly widens the average joint across the region.
6. **Region edits** — adding / removing / reordering regions never leaves
   orphan flagstone objects; removing the region removes its flagstones;
   switching a region between Boulder Field and Flagstone Paving cleanly
   swaps one object type for the other.
7. **Merge** — click *Merge Flagstones into Tile*: the loose object
   disappears, the tile vertex count jumps, and the result stays manifold
   (no warning).
8. **Export** — *Export Tiles to STL* includes the flagstones (loose if not
   merged, unioned if merged); a tile with a scatter region is treated as
   custom.
