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
object, rename its mesh, record the mesh's lowest local-space vertex Z
(`min_z`, used to seat the tree's true base rather than its bounding-box
origin), then discard the import shell and keep the mesh.

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
