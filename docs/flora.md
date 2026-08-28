# Flora — click-to-plant tree scattering

**Flora** plants real tree meshes onto a tile, chosen by hand rather than by a
procedural region: move the mouse over a tile and left-click to plant one
tree at the raycast hit point, picked at random from the current Tree Type's
STL asset folder, rotated a random amount around its vertical axis, and
scaled by a random variation factor.

This page covers the mesh caching, the scene tree it builds, and the manual
checklist for the bpy-only parts of `flora.py` itself (mesh caching/placement
math all needs `bpy`, so this module has no automated test coverage, the same
convention as `regions.py`/`scatter.py`). The tree-base-pad *geometry* it
triggers, however, lives in the bpy-free `tree_pads.py` and is unit-tested in
`tests/test_tree_pads.py` — see "Base flattening (tree pads)" below.

## Data model

One `HexFinityFloraPlacement` entry per planted tree, stored on the tile
(`obj.hexfinity_tile.flora_placements`), mirroring the minimalism of
`HexFinitySurfacePoint`:

| Field | Meaning |
|---|---|
| `species_file` | STL filename of the planted tree mesh |
| `tree_type` | Enum id of the flora folder this placement was planted from, stored explicitly at plant time so a later Tree Type dropdown change can't reinterpret old placements against the wrong folder |
| `local_x_mm` / `local_y_mm` | Tile-local planting position |
| `rotation_rad` | Random Z rotation, sampled once at plant time |
| `scale_factor` | Random size variation (around 1.0), sampled once at plant time |

Placements are stored **data**, not re-randomized on rebuild — a rebuild
reproduces the same trees, just re-seated to the current surface. The
man-height global scale (`map_props.man_height_mm / TREE_ASSET_MAN_HEIGHT_MM`)
is applied on top of `scale_factor` at sync time, not baked into it, so
changing Man Height rescales every existing tree.

No footprint/bounding-box field is stored per placement — a tree's footprint
is entirely derived from `species_file` + `scale_factor` (plus the global
man-height scale) at the moment it's needed, by `_get_or_import_mesh`. See
"Overlap avoidance" below.

## Mesh library + caching (`flora.py`)

Each species is imported from its STL **once per Blender session** and
cached as a single shared `bpy.types.Mesh` datablock (`use_fake_user=True`
keeps it alive while cached-but-unattached). Every planted tree is a
separate `Object` pointing at that same shared mesh (a Blender "linked
duplicate"), never a per-placement `.copy()` — the STLs are several MB each,
so sharing keeps a tile with many trees cheap.

`_get_or_import_mesh(tree_type, filename)` does the caching: a cache hit
first checks `mesh.name in bpy.data.meshes` (a plain Python dict of live
references can go stale across a `.blend` reload) before returning; a cache
miss uses the same import idiom as `HEXFINITY_OT_import_terrain_object` —
snapshot `scene.objects`, call `bpy.ops.wm.stl_import`, diff to find the new
object, rename its mesh, walk its vertices once to record the mesh's lowest
local-space vertex Z (`min_z`, used to seat the tree's true base rather than
its bounding-box origin), its local-space XY bbox half-extents/center
(`half_x`/`half_y`/`local_cx`/`local_cy`, the footprint used by the overlap
check below), *and* its true flat-base-cut radius (`base_radius` — the max
XY distance from the centroid of every vertex within an epsilon of `min_z`,
falling back to `15%` of the larger bbox half-extent if the base is a single
point), then discard the import shell and keep the mesh. All three are
cached per filename (`_mesh_min_z`, `_mesh_footprint_xy`, `_mesh_base_radius`)
alongside the mesh itself, evicted together on the same stale-reference path.
`base_radius` sizes each tree's flatten pad (see "Base flattening" below) —
self-tuning per species, with no separate radius slider to keep in sync.

## Overlap avoidance

A new placement is rejected — a `self.report({'WARNING'}, ...)` and no state
change — if its footprint would overlap an already-planted tree's, so that
*Export Tiles to STL* never produces two intersecting trees. The check runs
in `_place_tree`, right before the placement is stored, and is gated by the
scene-level **Avoid Overlap** toggle (default on); when on, an extra **Min
Spacing (mm)** slider adds required clearance beyond just not touching.

Each tree's footprint is treated as an **oriented bounding box**: its local
XY bbox (`half_x`/`half_y`/`local_cx`/`local_cy` from `_get_or_import_mesh`),
rotated by its own `rotation_rad` and scaled by
`scale_factor * (man_height_mm / TREE_ASSET_MAN_HEIGHT_MM)`, positioned at
its plant point. Two footprints are tested with
`procedural_surfaces.obb_overlap` — a bpy-free, unit-tested separating-axis
test over the 4 face-normal axes of the two rectangles (see
`tests/test_procedural_surfaces.py`).

The candidate is checked against every placement on the **current tile and
its 6 grid neighbours** (`_nearby_placements`, using the same bpy-free
`map.neighbour_coord`/`map.find_tile` helpers `SHARED_CORNERS` propagation
uses) — a tree near a tile seam can otherwise overlap into the next tile's
print, which a same-tile-only check would miss. Each existing placement's
footprint is recomputed the same way (`_placement_footprint`), using *its
own* tile's `matrix_world` for position; tile objects are placed by
translation only (never rotated), so a placement's stored `rotation_rad` is
already its world-space angle.

## Sync (`sync_flora`, `purge_flora`)

`sync_flora(tile_obj)`, called from `operators.rebuild_tile` right after the
scatter block:

1. Resolve `(mesh, min_z)` per placement via `_get_or_import_mesh`.
2. Raycast straight down at `(local_x_mm, local_y_mm)` against the tile's
   evaluated mesh — the same one-shot idiom as `scatter._make_z_of` — to
   find the current surface Z (falls back to `base_thickness_mm` on a miss,
   e.g. a placement stranded past a since-shrunk rim).
3. Create `Flora_<tile>_<i>` linked to the shared mesh, parented to the tile
   (identity `matrix_parent_inverse`, same as scatter boulders), rotated
   about Z only, scaled by `scale_factor * global_scale`, and sunk into the
   surface by the scene's **Penetration** slider.

`purge_flora(tile_obj)` removes every `hf_flora_of`-tagged child — mirrors
`scatter.purge_scatter` — and, like it, **never removes the mesh
datablock**, since it's shared/cached, not per-object. `rebuild_tile` fully
purges and resyncs a tile's flora on every rebuild (same cost model already
accepted for scatter), after the new mesh is assigned and the depsgraph
updated so the seating raycast samples the current surface.

## Base flattening (tree pads)

Every tree is forced perfectly world-vertical (`obj.rotation_euler`'s X/Y are
hard zero in `sync_flora`), but the tree assets have a flat base cut. On
sloped terrain a flat base only touches a sloped surface along one edge — the
uphill side pokes through the mesh, the downhill side floats above it. Tilting
the tree to match the slope was ruled out (it would make the trunk lean), so
instead the *terrain* gets a small flat "pad" under the tree, blended
smoothly back into the surrounding surface.

This is implemented as local, adaptive mesh refinement, not a uniform bump in
subdivision — the default top-vertex spacing (~12.5 mm) is far coarser than a
typical tree base (1–3 mm), and raising *Smoothness Passes*/*Resample
Density* enough to resolve that everywhere would blow up the tile's vertex
budget. The bpy-free `tree_pads.py` module instead refines only the
triangles near each planted tree:

1. `flora.pad_specs(tile_obj)` turns the tile's `flora_placements` into a
   list of `{"x", "y", "radius_mm", "blend_mm"}` dicts — `[]` immediately
   (no STL import) when there are no placements or **Flatten Base** is off.
   `radius_mm` is the species' cached `base_radius`, scaled by the
   placement's own `scale_factor * global_scale` and a `PAD_MARGIN = 1.25`
   safety factor so the pad comfortably covers the whole base.
2. `operators.rebuild_tile` computes this list and passes it as
   `mesh_builder.build_hex_tile`'s `flora_pads` kwarg, *before* the build —
   unlike scatter/flora object sync (which runs after), pad geometry has to
   be baked into the mesh itself.
3. Inside the builder, `tree_pads.refine_and_flatten` runs after brush/
   procedural-surface displacement and before top-face emission: it
   per-edge-splits triangles near a pad (crack-free by construction — a
   split decision is shared by both faces on an edge, never decided
   per-triangle) up to a small cap, appending new vertices strictly *after*
   the existing top-vertex range so `top_vertex_count()` and the
   `hf_brush_disp`/`hf_snap_disp` layers are completely unaffected by
   planting or unplanting a tree. It never splits a rim edge, so the side
   wall's n-gon is untouched too.
4. Every vertex within `radius_mm` of a pad centre is then **lerped**
   (not additively offset) toward a height sampled from the surface *before*
   flattening, with a smoothstep falloff over `pad_blend_mm` and a
   rim-edge-distance fade (mirroring the skirt fade in
   `operators._compute_snap_gap`) so a pad near a hex edge shrinks rather
   than desyncing the seam with the neighbouring tile. Because it's a lerp,
   the pad interior is flat even where a procedural-surface texture or brush
   stroke would otherwise bump it — the pad simply overrides whatever was
   there.

**Penetration** still sinks the tree slightly into its now-flush pad
(default lowered from 2.0 mm to 0.3 mm since its old job — hiding a slope
gap — is superseded by the pad; its remaining job is avoiding a
zero-thickness, z-fighting contact between a perfectly flat tree base and a
perfectly flat pad).

## Scene tree

```
HexFinity Map (collection)
├─ HexTile_00_00              (the tile object)
└─ Flora (collection)
   ├─ Flora_HexTile_00_00_000  (parented under the tile; linked mesh)
   └─ Flora_HexTile_00_00_001
```

The Flora collection is nested under the map's root collection — the first
sub-collection in the codebase — so planted trees stay organized in the
Outliner while remaining parented to their tile for correct world
transform, exactly like scatter boulders and terrain objects.

## Manual checklist (the bpy-only parts)

1. **Plant** — generate a map, select a tile, open the Flora box, press
   *Flora*, and left-click several spots on the tile. Each click adds a
   `Flora_<tile>_<i>` object at the hit point with a random species,
   rotation, and scale.
2. **Mesh sharing** — in the Outliner, confirm several planted trees of the
   same species point at one shared mesh datablock (multiple object users),
   not one mesh each.
3. **Penetration** — raise/lower the Penetration slider and confirm the
   trees visibly sink deeper/shallower into the surface.
4. **Collection** — confirm the Flora collection appears nested under the
   map's root collection, not loose in the scene.
5. **Re-seating** — edit the tile's corner heights, paint terrain with the
   Terrain Brush, or change subdivision — the planted trees move with the
   ground instead of floating or burying.
6. **Clear Map** — press Clear; confirm the Outliner has no leftover Flora
   collection or objects.
7. **Export** — *Export Tiles to STL* includes the planted trees
   automatically (they're ordinary mesh children of the tile, picked up the
   same way scatter boulders and terrain objects are).
8. **Packaging** — build via `deploy.ps1` and confirm the zip contains
   `hexfinity/assets/leefytree/*.stl`.
9. **Overlap avoidance** — with *Avoid Overlap* on (default), plant a tree,
   then click a spot close enough to overlap it: confirm the click is
   rejected with a "Too close to another tree" warning and no new object
   appears. Toggle *Avoid Overlap* off and repeat the same click: confirm it
   now succeeds. Turn it back on, raise *Min Spacing (mm)*, and confirm two
   trees that previously placed cleanly next to each other now get rejected
   until moved further apart. Finally, plant a tree near a tile edge, then
   switch to the neighbouring tile and click a mirrored spot close enough to
   overlap across the seam: confirm it's rejected too.
10. **Base flattening** — raise a corner so the tile is sloped, then plant a
    tree near the raised side with *Flatten Base* on (default): the terrain
    tessellates a small flat pad under it that blends smoothly outward, and
    the tree sits flush and level rather than poking through on the uphill
    side. Toggle *Flatten Base* off — the pad's extra vertices disappear and
    the old sunken-in look returns; toggle it back on and the pad returns.
    Drag *Pad Blend (mm)* and *Penetration (mm)* and confirm both re-seat the
    tile/tree live. Drag a corner slider with the tree still planted — the
    pad follows the new surface and the tree stays flush. Plant a tree near a
    hex edge and confirm the seam with the neighbour tile stays aligned (the
    pad's blend fades out near the rim rather than desyncing it). Export that
    tile to STL and confirm it's still a valid manifold. A headless smoke
    test of this whole path (plant → pad → rebuild → property-update
    callbacks) lives in `tests/_headless_flora_pad_check.py`.
