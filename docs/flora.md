# Flora — click-to-plant tree scattering

**Flora** plants real tree meshes onto a tile, chosen by hand rather than by a
procedural region: move the mouse over a tile and left-click to plant one
tree at the raycast hit point, picked at random from the current Tree Type's
STL asset folder, rotated a random amount around its vertical axis, and
scaled by a random variation factor.

This page covers the mesh caching, the scene tree it builds, and the manual
checklist for the bpy-only parts (there is no bpy-free portion — mesh
caching/placement math all needs `bpy`, so this module has no automated test
coverage, the same convention as `regions.py`/`scatter.py`).

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
its bounding-box origin) *and* its local-space XY bbox half-extents/center
(`half_x`/`half_y`/`local_cx`/`local_cy`, the footprint used by the overlap
check below), then discard the import shell and keep the mesh. Both are
cached per filename (`_mesh_min_z`, `_mesh_footprint_xy`) alongside the mesh
itself, evicted together on the same stale-reference path.

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
