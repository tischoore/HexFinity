# Terrain Objects — import & snap an STL onto a tile

## Context

HexFinity generates interlocking hex terrain tiles but has no way to place
decorative/prop meshes on them. The request: a **"Terrain Objects"** button in
the N-panel that, when exactly one tile is selected, opens a file dialog to
import an STL and drops it onto the **center of that tile**, snapping it down
onto the top surface (Z-only) without changing the STL's orientation.

This is feasible with no architectural changes: `panel.py` and `operators.py`
already import `bpy`; Blender 5.1 ships a native STL importer
(`bpy.ops.wm.stl_import`); and tiles already expose center XY
(`center_x_mm`/`center_y_mm`), world origin (`obj.location`), and a baked top
surface we can query exactly with `Object.ray_cast`. The bpy-free modules
(`mesh_builder.py`, `map.py`, `manifold_check.py`) are untouched.

Confirmed behavior (from clarifying Q&A):
- **Snap anchor**: STL's bounding-box *bottom* (min Z) rests flush on the surface.
- **Linkage**: the imported object is **parented to the tile** (a child in the
  scene hierarchy) so it travels with the tile. Z is snapped once at import time;
  it is not re-snapped on later height edits.
- **Snap control**: a "Snap to surface" checkbox in the dialog, **on by default**;
  when off, place at tile-center XY and leave the imported Z as-is.
- **Boundary rule**: a terrain object must not cross the hex boundary, but it
  *may* be tangent to it — so a feature can continue onto a neighbouring tile.
  Enforcement is a **soft warning** measured against the object's **XY
  bounding box**: if the bbox crosses an edge, place it anyway and
  `report({'WARNING'}, ...)`. Touching an edge (tangent) is allowed, not warned.

## Implementation

### 1. New operator — `hexfinity/operators.py`

Add `HEXFINITY_OT_import_terrain_object`, modeled on the existing operator
patterns (`HEXFINITY_OT_regenerate_map`, operators.py:226). Key points:

- **Properties**:
  - `filepath: StringProperty(subtype='FILE_PATH')`
  - `filter_glob: StringProperty(default="*.stl", options={'HIDDEN'})` — limits the
    browser to STL files.
  - `snap_to_surface: BoolProperty(default=True, name="Snap to surface")`
- **`poll(cls, context)`**: enabled only when the map is generated AND exactly one
  tile is selected:
  ```python
  sel = context.selected_objects
  return (context.scene.hexfinity_map.is_generated
          and len(sel) == 1 and sel[0].hexfinity_tile.is_generated)
  ```
- **`invoke(self, context, event)`**: `context.window_manager.fileselect_add(self)`
  then `return {'RUNNING_MODAL'}`. The `snap_to_surface` checkbox shows
  automatically in the file browser's operator sidebar; add a small `draw()` if we
  want to control its placement.
- **`execute(self, context)`**:
  1. Resolve the selected tile = `context.selected_objects[0]` (re-check it's a
     tile; `report({'ERROR'})` + `{'CANCELLED'}` otherwise).
  2. Snapshot existing object names, then call
     `bpy.ops.wm.stl_import(filepath=self.filepath)`. Identify the newly imported
     object(s) by diffing `context.scene.objects` before/after (the importer also
     leaves them selected/active as a fallback). Orientation is left at import
     default — we never touch rotation.
  3. **Target XY** = the tile center in world space. Tile meshes carry only a
     translation (`obj.location`, set at operators.py:188), so:
     `center_world = tile.matrix_world @ Vector((p.center_x_mm, p.center_y_mm, 0))`
     where `p = tile.hexfinity_tile`.
  4. Position the import so its bounding-box center aligns to `center_world` in
     XY (compute world bbox from `imported.bound_box` × `imported.matrix_world`).
  5. **Snap Z** (when `snap_to_surface`): query the exact surface via raycast on
     the tile mesh in *tile-local* space, from above the center straight down:
     ```python
     hit, loc, _n, _i = tile.ray_cast((p.center_x_mm, p.center_y_mm, BIG_Z),
                                      (0, 0, -1))
     ```
     `surface_world_z = (tile.matrix_world @ loc).z` on hit. Fallback if it misses
     (e.g. degenerate): the apex formula already used by the gizmo —
     `base_thickness_mm + level*level_height_mm` (gizmo.py:194 `_apex_z_mm`, minus
     the gizmo's cosmetic `+1`). Then shift the import so its world bbox-min Z ==
     `surface_world_z` (Z-only translation; XY and orientation unchanged).
  6. **Boundary check (soft warning)**: convert the object's world XY bounding-box
     corners into tile-local XY (tiles are translation-only, so local = world −
     `tile.location`), and test each against the six hex half-planes — the same
     normals as `clamp_center_to_hexagon` (mesh_builder.py:43):
     `n_i = (cos(π/6 − i·π/3), sin(π/6 − i·π/3))`, with
     `apothem = (diameter_mm/2)·√3/2`. If any corner has `n_i · p > apothem + ε`
     for some edge, `report({'WARNING'}, "extends past the hex boundary")` — but
     still place. `ε` (~1e-4 mm) lets a tangent edge pass without a warning. (Uses
     `safety_mm = 0`: tangency is explicitly allowed.)
  7. **Parent to the tile**: `imported.parent = tile`,
     `imported.matrix_parent_inverse = tile.matrix_world.inverted()`, then assign
     the computed world transform via `imported.matrix_world` so the visual
     position is preserved while the object becomes the tile's child. Move it into
     the tile's collection (`scene.hexfinity_map.root_collection`) so it lives
     beside the tile rather than in the scene's default collection (unlink from the
     importer's target collection, link to `root_collection`).
  8. `report({'INFO'}, ...)` and `return {'FINISHED'}`.

`bl_options = {'REGISTER', 'UNDO'}` so the placement is undoable. No re-entrancy
guard needed — this doesn't write tile properties, so it won't trigger
`rebuild_tile`.

> Note: parenting is the only change from a plain import; we do **not** add a
> re-snap-on-edit hook. If the tile's height/center later changes, the child keeps
> its baked Z (it still moves rigidly with the tile via the parent transform).

### 2. Panel button — `hexfinity/panel.py`

In the per-tile section (inside/after the editing `box`, ~panel.py:50-77), add:
```python
box.operator("hexfinity.import_terrain_object",
             text="Terrain Objects", icon='IMPORT')
```
The operator's `poll` disables it automatically unless exactly one tile is
selected. (Optional: also gate visibility on `len(context.selected_objects)==1`
for a clearer UI, but `poll` already handles the enabled state.)

### 3. Register — `hexfinity/__init__.py`

Add `operators.HEXFINITY_OT_import_terrain_object` to the `_classes()` tuple
(__init__.py:10-20), alongside the other operators.

## Scene hierarchy

Terrain objects become **children of the tile object** they snap to, living in the
same map collection. Two relationships are in play and they are independent:

- **Collection membership** (the Outliner's "Scene Collection" tree) — organizes
  what's linked where.
- **Object parenting** (`obj.parent`) — drives the transform; a child moves/rotates
  with its parent.

Both point at the tile, so the Outliner reads cleanly:

```
Scene Collection
└── HexFinity            (root_collection = scene.hexfinity_map.root_collection)
    ├── HexTile_00_00          ← tile object (parent)
    │   └── Tree_A             ← imported STL, parent = HexTile_00_00
    ├── HexTile_01_00
    │   ├── Rock_01            ← multiple terrain objects per tile are allowed
    │   └── Rock_02
    └── HexTile_00_01
```

- Each imported object's `parent` is the tile; its `matrix_parent_inverse` is set
  to `tile.matrix_world.inverted()` so the world transform is preserved at the
  moment of parenting.
- Dragging the tile's center gizmo or moving the tile moves its terrain children
  rigidly (parent transform). Tile mesh *rebuilds* (`rebuild_tile`) replace the
  tile's mesh data but keep the object — so the parent link and children survive a
  rebuild.
- Deleting a tile (e.g. Regenerate Map) takes its terrain children with it,
  matching the "belongs to this tile" intent.

## Critical files

- `hexfinity/operators.py` — new operator (the bulk of the work).
- `hexfinity/panel.py` — one `layout.operator(...)` line.
- `hexfinity/__init__.py` — one entry in `_classes()`.

## Notes / decisions

- **XY anchor**: align the STL's bounding-box center to the tile center (natural
  "over the center"). Simple to change to object-origin if preferred.
- **Boundary enforcement is intentionally soft**: bounding-box test, warn-only.
  A model authored to meet an edge (so a feature continues on the neighbour) is
  expected to sit tangent to the boundary — that is allowed and not warned. The
  bbox test is conservative (it can warn on a model that technically fits after
  rotation); acceptable since it never blocks placement.
- **Units**: the plugin works in mm-as-Blender-Units; `stl_import` uses
  `global_scale=1.0`, importing STL coordinates as-is — consistent.
- **API confirmation**: `bpy.ops.wm.stl_import` is the native importer in Blender
  4.0+/5.x (replaced the old `io_mesh_stl` addon). Confirm the exact operator id
  in the target 5.1 build during implementation; the rest (`ray_cast`,
  `bound_box`, `fileselect_add`) are stable core APIs.

## Verification

1. Build/deploy with `deploy.ps1` (junction into Blender user extensions for live
   dev).
2. In Blender: generate a map, select **one** tile → the "Terrain Objects" button
   is enabled; select zero or multiple → it's disabled.
3. Click it, pick an STL → it imports, sits centered over the tile with its bottom
   flush on the top surface, orientation unchanged.
4. Confirm in the Outliner that the imported object appears **nested under the
   tile** (parent) inside the HexFinity collection. Move the tile → the terrain
   object moves with it.
5. Test on a tile with varied corner heights and a dragged/offset center to
   confirm the raycast snap tracks the actual surface, not a flat estimate.
6. Import a model wider than the hex → it is placed and a boundary **warning** is
   shown. Import one sized to touch an edge → no warning (tangent allowed).
7. Toggle "Snap to surface" off → object lands at center XY with its imported Z.
8. Undo (Ctrl-Z) removes the imported object cleanly.
9. Run the pytest suite to confirm the bpy-free modules are untouched:
   `"C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pytest tests -v`
