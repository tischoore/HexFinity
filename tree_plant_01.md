# Flora brush — plant trees from the `leefytree` asset library

## Context

`flora.py` currently only implements a preview marker: a modal operator that
raycasts the mouse onto the map and draws a live yellow circle at the hit
point (`HEXFINITY_OT_flora_marker`), with **no `LEFTMOUSE` handling and no
placement logic** — explicitly "foundation only" per its docstring and
CLAUDE.md. A companion scene property, `HexFinityFloraProperties.tree_type`,
already exists as a stub enum (`'LEAFY_TREE'`) but nothing reads it yet. The
same commit (`710846d`) added `assets/leefytree/` — 10 STL tree meshes
("LeafyTree_Small_1..10.stl", ~6.5–6.9MB each) authored at the project's
man-height scale convention (10mm — the default of `HexFinityMapProperties
.man_height_mm`) — plus two design docs (`tree_painter.md`, `tree_gen_plan.md`)
that explored much more ambitious directions (socket-carved removable/
printable pegs; fully procedural branching trees). Neither matches what's
being asked for now: those are bigger, separate ideas for later, not
prerequisites. This plan implements the simpler, first working version the
user described directly: click to plant a real tree mesh, randomly chosen
from the asset folder, randomly rotated and scaled, sunk into the surface by
a configurable amount — reusing the existing scatter/terrain-object/brush
patterns rather than the socket-carving machinery.

`## 8` below later revisits removability, but via a different mechanism than
`tree_painter.md` proposed: a boolean-cut cylindrical socket (exact circle,
exact placement) instead of that doc's bpy-free vertex-fan carve (polygon
hole snapped to the nearest mesh vertex).

Decisions confirmed with the user:
- **Single click plants one tree** (not a drag-painted stroke).
- **Each planted tree is rotated randomly around its height (Z) axis** at
  plant time — sampled uniformly in `[0, 2π)`, stored on the placement
  (`rotation_rad`), and re-applied unchanged on every rebuild/re-seat (not
  re-randomized), matching the scale-variation decision below.
- Tree objects live in a **new "Flora" Blender Collection**, nested under the
  map's root collection (first sub-collection in the codebase — organizes
  the Outliner; objects still get parented to their tile for correct world
  transform, exactly like scatter boulders).
- Placements **persist per-tile and re-seat on rebuild** (corner edits,
  terrain-brush strokes, subdivision changes all re-raycast existing trees
  onto the new surface, like the brush/snap displacement layers already do).
- **Penetration is a fixed mm depth** (mirrors `scatter.py`'s `SINK_MM`
  constant, just exposed as a live slider instead of hardcoded).

Also answers the "can we reference instead of copy" question: yes — each of
the (up to) 10 species is imported from STL **once** per Blender session and
cached as a single shared `bpy.types.Mesh` datablock; every planted tree is a
separate `Object` pointing at that same shared mesh (Blender's own
"linked-duplicate" mechanism), not a per-placement mesh copy. This matters
given the file sizes (~7MB/high-poly STL each).

## 1. Asset packaging fix

`deploy.ps1` only zips `hexfinity/*` (`deploy.ps1:19,43`) into the shipped
extension — `assets/leefytree/` currently sits at the **repo root**, outside
`hexfinity/`, so it would silently be missing from any built/installed
extension. Move it: `git mv assets/leefytree hexfinity/assets/leefytree`.
No `deploy.ps1` or `blender_manifest.toml` changes are needed beyond the
move — the manifest has no file-inclusion config to update, and the folder
becomes part of `hexfinity/*` automatically.

## 2. Data model (`hexfinity/properties.py`)

- New `HexFinityFloraPlacement(bpy.types.PropertyGroup)` (per-tile, mirrors
  `HexFinitySurfacePoint`'s minimalism): `species_file` (StringProperty — the
  STL filename), `tree_type` (StringProperty — the enum id of the folder it
  came from at plant-time, stored explicitly rather than re-read from the
  live scene dropdown later, so a future dropdown change can't reinterpret
  old placements against the wrong folder), `local_x_mm`/`local_y_mm`
  (tile-local mm), `rotation_rad`, `scale_factor` (the *variation* factor
  only, default 1.0 — the man-height global scale is applied separately at
  sync time so it stays live-editable).
- Add `flora_placements: bpy.props.CollectionProperty(type=HexFinityFloraPlacement)`
  to `HexFinityProperties`, next to the existing `surface_regions` (`properties.py:479`).
- Extend `HexFinityFloraProperties` (`properties.py:123-131`) with:
  `scale_variation_pct` (FloatProperty, `subtype='PERCENTAGE'`, default 20,
  0–100 — ± jitter around 1.0) and `penetration_mm` (FloatProperty, default
  2.0, min 0 — mirrors `scatter.SINK_MM`'s role but user-editable).
- Add `flora_collection: bpy.props.PointerProperty(type=bpy.types.Collection,
  options={'HIDDEN'})` to `HexFinityMapProperties`, next to `root_collection`
  (`properties.py:103-107`) — same get-or-create-once pattern.
- Register `HexFinityFloraPlacement` in `__init__.py`'s `_classes()` list,
  ordered before `HexFinityProperties` (same convention as
  `HexFinitySurfacePoint`/`HexFinitySurfaceRegion`).

## 3. Mesh library + caching (`hexfinity/flora.py`)

Grows `flora.py` from marker-only into the full flora subsystem — same shape
as `scatter.py` bundling both `sync_scatter` and the merge operator in one
file.

```python
_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_TREE_TYPE_FOLDERS = {'LEAFY_TREE': "leefytree"}   # one entry per HexFinityFloraProperties.tree_type item
TREE_ASSET_MAN_HEIGHT_MM = 10.0   # the man-height scale the STLs were authored at
FLORA_OF = "hf_flora_of"

_species_cache = {}   # tree_type -> sorted [filenames]
_mesh_cache = {}       # filename -> bpy.types.Mesh (shared, use_fake_user=True)
_mesh_min_z = {}       # filename -> float (lowest local-space vertex Z)
```

- `_list_species(tree_type)` — lazily globs `*.stl` in that type's folder,
  cached.
- `_get_or_import_mesh(tree_type, filename)` — cache hit: return
  `(mesh, min_z)`, but first check `mesh.name in bpy.data.meshes` (the cache
  dict is a plain Python dict of live datablock references; opening a
  different .blend file or reloading the addon can leave it pointing at
  freed data, so this staleness check is the guard, evicting on failure).
  Cache miss: same import idiom as `HEXFINITY_OT_import_terrain_object`
  (`operators.py:640-649`) — snapshot `scene.objects`, call
  `bpy.ops.wm.stl_import(filepath=...)`, diff to find the new object(s).
  Take `imported[0].data`, rename it (`f"HF_Flora_{Path(filename).stem}"`),
  set `mesh.use_fake_user = True` (keeps it alive while cached-but-unattached
  — a plain dict reference doesn't count toward Blender's own datablock user
  count), read `min_z = min(v.co.z for v in mesh.vertices)`, then
  `bpy.data.objects.remove(imported[0], do_unlink=True)` (drops the import
  shell, keeps the mesh). Defensively remove any extra imported objects too.
- `purge_flora(tile_obj)` — mirrors `scatter.purge_scatter`: remove every
  child tagged `FLORA_OF`. **Never** remove the mesh datablock here (unlike
  `scatter._remove_object`) — it's shared/cached, not per-object.
- `ensure_flora_collection(context)` — get-or-create `map_props
  .flora_collection`: if unset or its name is no longer in `bpy.data
  .collections`, create `bpy.data.collections.new("Flora")`, link it as a
  child of `map_props.root_collection`, store the pointer.
- `sync_flora(tile_obj)` — for each `tile_obj.hexfinity_tile.flora_placements`
  entry: resolve `(mesh, min_z)` via `_get_or_import_mesh`; raycast straight
  down at `(local_x_mm, local_y_mm)` against `tile_obj`'s evaluated mesh
  (same one-shot idiom as `scatter._make_z_of`, fallback to
  `map_props.base_thickness_mm` on a miss — e.g. a placement stranded past a
  since-shrunk rim); compute `global_scale = map_props.man_height_mm /
  TREE_ASSET_MAN_HEIGHT_MM`, `total_scale = p.scale_factor * global_scale`;
  create `bpy.data.objects.new(f"Flora_{tile_obj.name}_{i:03d}", mesh)`
  (shared mesh — no `.copy()`), link into the Flora collection, `obj.parent
  = tile_obj` (default identity `matrix_parent_inverse`, same as
  `scatter.sync_scatter` — tile-local mm interpreted correctly because the
  tile itself carries only translation), `obj.rotation_euler = (0, 0,
  p.rotation_rad)` (Z-only — tree always points straight up), `obj.scale =
  (total_scale,) * 3`, `obj.location = (local_x_mm, local_y_mm, surface_z -
  penetration_mm - min_z * total_scale)`, tag `obj[FLORA_OF] = True`.

## 4. Click-to-place (`flora.py`, `HEXFINITY_OT_flora_marker`)

- Extend `_update_hit` (`flora.py:81-96`) to also store `self._hit_tile =
  hit_obj.original if hit else None` alongside `self._hit_world`, so the
  click handler reuses the already-computed hit instead of re-raycasting.
- Add a branch in `modal()` (`flora.py:64-79`):
  `if event.type == 'LEFTMOUSE' and event.value == 'PRESS': self._place_tree(context); return {'RUNNING_MODAL'}`
  (stays in the modal loop — Esc/RMB still closes it, so multiple trees can
  be planted in one activation).
- `_place_tree(context)`: bail if `self._hit_tile is None`; convert
  `self._hit_world` to tile-local mm via `self._hit_tile.matrix_world
  .inverted() @ self._hit_world`; pick species via `random.choice(_list_species
  (flora_props.tree_type))` (report a warning + bail if the folder is empty);
  append a new `HexFinityFloraPlacement` to `tile.hexfinity_tile
  .flora_placements` with that species, `tree_type`, local XY,
  `random.uniform(0, 2*pi)` rotation, and `1.0 + random.uniform(-pct, pct) /
  100.0` scale (pct from `flora_props.scale_variation_pct`); call
  `operators.rebuild_tile(tile)`; `bpy.ops.ed.undo_push(message="Flora: plant
  tree")` (one undo step per click, same as `brush._end_stroke`'s pattern —
  Ctrl+Z removes a mis-click since there's no separate remove mode in this
  first pass).

## 5. Rebuild integration (`hexfinity/operators.py`)

In `rebuild_tile` (`operators.py:94-232`), right after the existing scatter
block (`operators.py:221-230`), add the analogous flora block:

```python
flora_placements = tile_props.flora_placements
has_flora = any(c.get(flora.FLORA_OF) for c in obj.children)
if len(flora_placements) > 0 or has_flora:
    from . import flora
    flora.purge_flora(obj)
    if len(flora_placements) > 0:
        bpy.context.view_layer.update()
        flora.sync_flora(obj)
```

Same full-purge-and-resync-every-rebuild shape as scatter — safe because
placements are stored data (species/position/rotation/scale), not
re-randomized, so a rebuild reproduces the same trees just re-seated to the
current surface. Runs inside the existing `_REBUILDING` guard.

`## 8` extends this same block with two more calls (socket cut + peg sync)
right after `flora.sync_flora(obj)`, and extends `purge_flora` to also strip
tree-base peg objects — see `## 8` for the full block.

In `HEXFINITY_OT_clear_map.execute` (`operators.py:428-445`), the flora
objects live in the new **Flora** sub-collection, not directly in
`root_collection.objects` (which only lists directly-linked objects, not a
nested collection's members) — so the current blanket loop misses them.
Add, before the existing `root_collection` teardown:

```python
flora_coll = map_props.flora_collection
if flora_coll is not None:
    for o in list(flora_coll.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    if flora_coll.name in bpy.data.collections:
        bpy.data.collections.remove(flora_coll)
map_props.flora_collection = None
```

## 6. Panel UI (`hexfinity/panel.py`)

Replace the current 3-line inline block (`panel.py:150-161`) with a proper
box, matching the Terrain Brush section's style:

```python
box = layout.box()
box.label(text="Flora", icon='OUTLINER_OB_POINTCLOUD')
box.prop(scene.hexfinity_flora, "tree_type")
box.prop(scene.hexfinity_flora, "scale_variation_pct")
box.prop(scene.hexfinity_flora, "penetration_mm")
if flora.is_active():
    row = box.row(); row.alert = True
    row.label(text="Flora active — Esc / RMB to close", icon='INFO')
else:
    box.operator("hexfinity.flora_marker", text="Flora", icon='OUTLINER_OB_POINTCLOUD')
```

## 7. Docs

- `CLAUDE.md`: update the `flora.py` module-map row (marker → full
  plant/cache/sync subsystem) and mention the `rebuild_tile`/`clear_map`
  additions in their existing rows. `## 8` extends this row further with
  socket-cutting/peg-generation and the new `HEXFINITY_OT_export_trees`
  operator.
- `README.md`: rewrite the "Flora marker (preview only)" section to describe
  actual placement, random species/rotation/scale-variation, and the
  penetration slider; add "Flora" to the scene-tree ASCII diagram
  (`README.md:356` area) as a sub-collection under the map collection.
- New `docs/flora.md`, mirroring `docs/boulder-field.md`'s structure (manual
  checklist — `flora.py` is a bpy module like `scatter.py`/`regions.py`, no
  automated test coverage per the existing convention): click to plant,
  confirm random species/rotation/scale and mesh-data sharing (Outliner
  shows the mesh datablock with multiple object users), confirm penetration
  depth visually, confirm the Flora collection nests under the map
  collection, confirm re-seating after a corner-height edit / terrain-brush
  stroke / subdivision change, confirm Clear Map removes the Flora
  collection + objects, confirm a tile STL export includes the planted
  trees (automatic via `HEXFINITY_OT_export_tiles`'s existing type-based
  `_mesh_children` pickup — no export code changes needed), and confirm a
  zip built via `deploy.ps1` actually contains
  `hexfinity/assets/leefytree/*.stl`. `## 8` adds further checklist items for
  the socket/peg mechanism and the new Export Trees output.

## 8. Removable trees — socket & peg

Planted trees should be **removable**: a cylindrical hole is cut into the
hex tile where each tree sits, and a matching "tree base" peg (trimmed
slightly smaller) is generated so a printed tree can be plugged into and
pulled back out of its tile.

`tree_painter.md` (see `## Context` above) already designed this idea once,
but via a bpy-free vertex-fan carve — hand-editing the mesh's triangle fan
around one *existing* subdivided vertex (mirroring `mesh_builder.py`'s
hand-built tab/hole interlock). That snaps tree placement to the nearest
mesh vertex and produces a k-sided polygon hole (matching local mesh
valence), not a true circle. This section instead uses a **boolean
DIFFERENCE cut**, done as a post-build bpy step exactly like `scatter.py`'s
existing `HEXFINITY_OT_merge_scatter` (which already does a boolean `UNION`
in this codebase — the only prior boolean-CSG precedent). That trades
`mesh_builder.py`'s "watertight by construction" guarantee for an exact
circular hole at the exact placement point, with the same graceful
warn-don't-hard-fail fallback `merge_scatter` already uses if the solver
ever produces a bad result.

Confirmed with the user:
- **Cut method**: boolean DIFFERENCE cylinder cutter (not the
  `tree_painter.md` vertex-fan carve).
- **Peg/tree relationship**: the peg ("tree base") stays a fully separate
  mesh object — not boolean-unioned onto the tree mesh. Two independent
  printable bodies.
- **Export**: a peg cannot be printed pre-inserted into an enclosed cavity,
  so tiles and trees(+pegs) must NOT be bundled into one nested multi-body
  STL. Tiles export their hole-bearing shell alone; trees+pegs get a new,
  separate export path.

Geometry parameters interpreted from the user's plain-English description
(implemented as simple named constants, trivial to retune):
- **Hole diameter**: 10.0mm, exact as given.
- **Hole depth**: from the tile's top surface (flush, open from above so the
  peg can be inserted/removed) down to 5mm below the tree's already-sunk
  lowest point — i.e. total depth = `penetration_mm + 5.0`. The "flush with
  the surface" top boundary wasn't explicit in the request but is the only
  reading under which "removable" is physically meaningful.
- **Peg trim**: read as a diametral reduction — peg diameter =
  `10.0 - 0.02` = 9.98mm (0.01mm clearance per side). Very tight for FDM
  printing (typical slip fits are 0.1–0.3mm) but implemented as one named
  constant, trivial to loosen later.
- **Peg height**: exactly the hole depth (`penetration_mm + 5.0`) — flush
  fit vertically, slip fit only on diameter.
- **New guard, not explicitly requested but necessary**: if a placement's
  local terrain is thin enough that the hole would come within
  `MIN_HOLE_FLOOR_MARGIN_MM` (1.0mm) of the tile's bottom face, that
  placement's cut (and its peg) is skipped with a reported warning rather
  than silently breaching the tile floor — a dynamic per-placement version
  of `mesh_builder.py`'s existing static
  `base_thickness_mm >= TAB_HEIGHT_MM + TAB_HOLE_TOLERANCE_MM` guard.

No new `HexFinityFloraPlacement` fields are needed — hole/peg geometry is
fully derived at sync time from a placement's existing
`local_x_mm`/`local_y_mm` plus the scene-wide `penetration_mm`, so the data
model stays as lean as the rest of flora.

### 8.1 New constants (`flora.py`)

```python
TREE_HOLE_DIAMETER_MM = 10.0
TREE_HOLE_DEPTH_BELOW_TREE_MM = 5.0
TREE_HOLE_OVERSHOOT_MM = 2.0   # cutter overshoots above the surface so the
                                # top breach is clean, not tangent — mirrors
                                # scatter.SINK_MM's rationale
TREE_HOLE_SEGMENTS = 24        # cylinder tessellation
TREE_PEG_CLEARANCE_MM = 0.02
MIN_HOLE_FLOOR_MARGIN_MM = 1.0
TREE_BASE_OF = "hf_tree_base_of"
_tree_base_mesh_cache = {}     # rounded penetration_mm -> shared bpy.types.Mesh
```

### 8.2 Shared raycast helper

Extract `_surface_z_at(tile_obj, local_x_mm, local_y_mm, fallback)` — the
same one-shot raycast idiom already used by `scatter._make_z_of` /
`operators.apply_terrain_snap` — so `sync_flora`, the new
`cut_tree_sockets`, and the new `sync_tree_bases` share one implementation
instead of each re-deriving it.

### 8.3 `cut_tree_sockets(tile_obj, placements, penetration_mm)`

- Per placement: `surface_z = _surface_z_at(...)`,
  `bottom_z = surface_z - penetration_mm - TREE_HOLE_DEPTH_BELOW_TREE_MM`.
  Skip (report a warning) if `bottom_z < MIN_HOLE_FLOOR_MARGIN_MM`.
- Build **one combined cutter mesh** for every accepted placement in a
  single `bmesh.new()` — per placement,
  `bmesh.ops.create_cone(bm, cap_ends=True, segments=TREE_HOLE_SEGMENTS, radius1=d/2, radius2=d/2, depth=top_z-bottom_z, matrix=Matrix.Translation((x, y, (top_z+bottom_z)/2)))`
  where `top_z = surface_z + TREE_HOLE_OVERSHOOT_MM`. One boolean apply per
  tile (not one per tree) — cheaper and matches `merge_scatter`'s
  single-pass style.
- Create a temp cutter object, add a `'BOOLEAN'` modifier on `tile_obj`
  (`operation='DIFFERENCE'`, `solver='EXACT'`, `.object = cutter_obj`),
  apply it via `bpy.ops.object.modifier_apply` inside a
  `context.temp_override(...)` — the exact pattern
  `HEXFINITY_OT_merge_scatter` already uses for its `UNION` — then delete
  the cutter object + mesh.
- Re-validate with `manifold_check.assert_two_manifold()`; on failure,
  report a warning and leave the result as-is (don't revert) — the same
  "warn, don't hard-fail" fallback `merge_scatter` already uses.

### 8.4 `sync_tree_bases(tile_obj, placements, penetration_mm)`

- `_get_or_build_tree_base_mesh(penetration_mm)`: cache key =
  `round(penetration_mm, 3)` (peg height depends on it). Build once via
  `bmesh.ops.create_cone` (diameter `TREE_HOLE_DIAMETER_MM - TREE_PEG_CLEARANCE_MM`,
  height `penetration_mm + TREE_HOLE_DEPTH_BELOW_TREE_MM`), set
  `mesh.use_fake_user = True`, cache — mirrors the existing species-mesh
  cache in `_get_or_import_mesh`.
- Per accepted placement (same skip as the cut — a skipped hole gets no
  peg): `bpy.data.objects.new(f"TreeBase_{tile_obj.name}_{i:03d}", mesh)`
  (shared, linked-duplicate — no `.copy()`), link into the Flora collection,
  parent to `tile_obj`, position at
  `(local_x_mm, local_y_mm, (surface_z + bottom_z) / 2)` (vertically
  centered in the hole), tag `obj[TREE_BASE_OF] = True`. No rotation needed
  — a plain cylinder is rotationally symmetric.

### 8.5 `purge_flora` extension

Extend the existing per-tile purge to also remove `TREE_BASE_OF`-tagged
children (same tag-based child removal already used for `FLORA_OF`) — never
removes the shared cached mesh datablock.

### 8.6 `operators.py` — `rebuild_tile` integration

Right after the existing flora sync block from `## 5`:

```python
if len(flora_placements) > 0 or has_flora:
    ...
    if len(flora_placements) > 0:
        bpy.context.view_layer.update()
        flora.sync_flora(obj)
        bpy.context.view_layer.update()
        flora.cut_tree_sockets(obj, flora_placements, flora_props.penetration_mm)
        flora.sync_tree_bases(obj, flora_placements, flora_props.penetration_mm)
```

Runs inside the existing `_REBUILDING` guard. Every rebuild fully re-cuts
and re-seats — the same full-purge-and-resync cost model already accepted
for scatter/flora.

### 8.7 Export (`operators.py`)

- `_mesh_children(obj)` gains a filter excluding `FLORA_OF`/`TREE_BASE_OF`-
  tagged children, so a tile's own STL export goes back to being its
  hole-bearing shell alone (other real terrain-object children unaffected).
- New `HEXFINITY_OT_export_trees` operator (new button in the panel's
  Export box, alongside `hexfinity.export_tiles`): walks every tile in the
  map, collects every flora tree object + its matching tree-base peg, and
  exports them together as one combined STL — every tree/peg pair is a
  separate disjoint body in that file, individually printable/separable,
  analogous to how `HEXFINITY_OT_export_tiles` already treats multiple mesh
  children as one multi-body STL.

### 8.8 Docs

- `CLAUDE.md`: extend the `flora.py` module-map row to mention socket
  cutting + peg generation; note the new `HEXFINITY_OT_export_trees`
  operator.
- Flora manual-checklist doc (`## 7`'s `docs/flora.md`): add checks for a
  visible hole under each planted tree, peg diameter close to 9.98mm, a
  too-thin terrain spot skipping the cut with a warning instead of
  breaching the tile, Clear Map removing tree-base objects, and the new
  Export Trees output containing one peg per placement.

### 8.9 Known limitations (documented, not solved this pass)

- Overlapping 10mm holes from trees planted very close together aren't
  explicitly prevented; the EXACT solver usually tolerates overlapping
  cutter islands but it isn't guaranteed — a minimum planting spacing could
  be added to the brush later if this proves to be a real problem.
- Every rebuild re-cuts every socket via a fresh boolean pass each time —
  the most likely source of slowdown on tiles with many planted trees.

## Verification

- No new bpy-free modules are introduced (mesh caching/placement math all
  needs `bpy`), so this isn't covered by the pytest suite — verify by running
  the existing suite unchanged (`pytest tests -v` via Blender's bundled
  Python) to confirm nothing else regressed, then work through the manual
  `docs/flora.md` checklist above in real Blender: generate a map, open the
  Flora box, click several spots on a tile (confirm varied species/
  rotation/scale and correct sink depth), edit that tile's corner heights or
  paint terrain and confirm trees re-seat instead of floating/burying,
  Clear Map and confirm the Outliner has no leftover Flora collection or
  objects, and export the tile to confirm the STL contains the tree bodies.
- For `## 8`: plant several trees, confirm each gets a visible cylindrical
  hole at its base; export the tile and confirm the STL shell has real
  holes; run the new Export Trees operation and confirm each peg is present
  and sized to slide into a 10mm hole; test a very thin/low terrain spot and
  confirm the cut is skipped with a warning instead of punching through the
  tile's bottom; Clear Map and confirm no orphaned tree-base objects remain
  in the Outliner.
