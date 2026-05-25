# Terrain Creation — Initial Plan

Extend HexFinity from a single-tile generator into an **X×Y map generator** where the resulting tiles tile seamlessly and share corner heights. All linear inputs remain in millimetres.

> **What "sharing a corner" means in this plan.** Each hex remains an independent mesh with its own six corner vertices — there is **no single merged vertex** at the seam. "Shared" is an *editing-time alignment relationship*: when a corner level on one tile changes, the matching corners on the (up to two) neighbour tiles that geometrically meet at that point are updated to the same level, so the rim height matches across the seam. The vertices stay separate per tile (which is necessary because each tile is its own watertight 2-manifold mesh); their *Z values* are kept equal through propagation.

This document is a *plan*, not the change itself. It captures the design choices that came out of the kickoff Q&A so the implementation can proceed without re-asking them.

**Map invariants (all hexes in a map share these):**
- **Diameter is uniform.** Every tile in the map has the same point-to-point diameter — this is what makes the tessellation close cleanly and is why `diameter_mm` lives on the Scene-level property group, not per-tile.
- **Level height, base thickness, and subdivisions are uniform** for the same reason: differing values would either break shared-corner heights or break side-wall vertex counts at the seam.
- **Per-tile values are corner levels (P1..P6) and the centre apex (override + XY).** These can differ from tile to tile without breaking the seam.

---

## 1. Confirmed design decisions (from kickoff Q&A)

| Topic | Decision |
|---|---|
| Hex orientation | **Flat-top** (re-labelling existing P1..P6). |
| Grid layout | **Offset columns** — columns are vertical, every other column shifted by half a row in Y. Concretely: **odd-q offset** (odd columns shifted +Y by `row_pitch / 2`). |
| X / Y semantics | **Counts of tiles**. `X = number of columns (q ∈ [0, X-1])`, `Y = number of rows (r ∈ [0, Y-1])`. If `X == 0` or `Y == 0`, generate a single tile at `(0, 0)`. |
| State location | **Scene PropertyGroup** (`bpy.types.Scene.hexfinity_map`). One map per scene, by design. Globals on this group (diameter, level height, base thickness, subdivisions) are **uniform across every tile in the map** — that is why they are not per-tile. |
| Corner sync | **Immediate, in update-callback**, guarded by a re-entrancy flag (same pattern as the existing `_REBUILDING` flag in `operators.py`). |
| Regenerate flow | "Regenerate" button replaces "Generate" once a map exists. Uses Blender's `invoke_confirm` for the are-you-sure dialog. Wipes the existing map collection and all its tiles, then rebuilds from current global params. |

---

## 2. Flat-top hex geometry & re-labelling

Currently the tile is **point-up** with `P1` at 12 o'clock and corners clockwise. For flat-top with the same clockwise ordering, the new corner positions are:

```
P1: upper-right   (R*cos 60°,  R*sin 60°)   = (R/2,        R*√3/2)
P2: right         (R,          0)
P3: lower-right   (R/2,       -R*√3/2)
P4: lower-left   (-R/2,       -R*√3/2)
P5: left         (-R,          0)
P6: upper-left   (-R/2,        R*√3/2)
```

Where `R = diameter_mm / 2` (point-to-point radius; for flat-top this is the **long** distance — corner-to-corner).
Apothem (corner-edge midpoint distance) is `a = R · √3 / 2` and is the **short** half-width of the tile.

Edge naming stays the same: `Ei = Pi → Pi+1`, wrapping `E6 = P6 → P1`. The top edge (12 o'clock) is now `E6` (P6→P1). The bottom edge is `E3` (P3→P4).

`mesh_builder.py` needs two angle starts to shift for flat-top:

1. The `angle` start that builds `corners_xy` shifts from `π/2` to `π/3` (corner positions).
2. The `theta` start inside `clamp_center_to_hexagon` shifts from `π/3` to `π/6` (outward edge normals). The clamp encodes the hex's six rim half-planes, so it is *not* coordinate-agnostic — without this second change the centre would be clamped to a point-up hex inscribed in the new flat-top one.

All other geometry — Coons-patch math, spoke/rim normal derivations, side-wall and bottom logic — is coordinate-agnostic in `(x, y)` and needs no changes.

**README and `docs/hex_anatomy.svg` will be updated** as part of this work (see §9 and §10). The existing SVG is rewritten for flat-top, and a second SVG is added that illustrates the assembled map (tessellation + shared corners).

---

## 3. Grid math (odd-q offset, flat-top)

```
R          = diameter_mm / 2
apothem    = R * √3 / 2
col_pitch  = 1.5 * R           # X distance between adjacent columns
row_pitch  = 2 * apothem       # Y distance between adjacent rows (= √3 * R)

def tile_world_xy(q, r):
    x = q * col_pitch
    y = r * row_pitch + (row_pitch / 2 if (q & 1) else 0.0)
    return (x, y)
```

The map is built with `tile (0, 0)` placed at the scene origin and the map extending in +X / +Y. Each tile mesh is generated **centred at its object origin**, and the per-tile `bpy.types.Object.location` is set to `tile_world_xy(q, r)`. This keeps the existing `mesh_builder.build_hex_tile()` API (centred at 0,0,0) unchanged.

---

## 4. Neighbour & shared-corner model

**Reminder on terminology.** "Shared corner" here means *the three tiles that meet at one geometric vertex keep their respective P-corner levels in lockstep* — not that they share a vertex in the mesh. Each tile keeps its own corner vertex; what is shared is the **level value** (and therefore the resulting Z). Vertex deduplication happens only *within* a single tile's mesh (rim/spoke/center keys in `mesh_builder.py`), never across tiles.

In a flat-top grid, three tiles meet at every interior vertex, so **each corner's level is mirrored on up to 2 other tiles**.

Direction vectors (odd-q offset) for the six neighbours of tile `(q, r)`:

```
                                even q          odd q
N  (north)                      (q,   r+1)      (q,   r+1)
NE (north-east)                 (q+1, r  )      (q+1, r+1)
SE (south-east)                 (q+1, r-1)      (q+1, r  )
S  (south)                      (q,   r-1)      (q,   r-1)
SW (south-west)                 (q-1, r-1)      (q-1, r  )
NW (north-west)                 (q-1, r  )      (q-1, r+1)
```

**Shared-corner table** — for tile `T = (q, r)`, the two neighbours that share each corner and which corner on each:

| Corner on T | Neighbour A      | Corner on A | Neighbour B      | Corner on B |
|---|---|---|---|---|
| P1 (upper-right)   | N    | P3 | NE   | P5 |
| P2 (right)         | NE   | P4 | SE   | P6 |
| P3 (lower-right)   | SE   | P5 | S    | P1 |
| P4 (lower-left)    | S    | P6 | SW   | P2 |
| P5 (left)          | SW   | P1 | NW   | P3 |
| P6 (upper-left)    | NW   | P2 | N    | P4 |

This table is the *entire* neighbour data structure required by the spec — it is small, static (depends only on flat-top geometry + odd-q offset), and lives as a constant in code (`SHARED_CORNERS` in `map.py`).

Tiles are looked up by `(q, r)` via a dict built on demand from the children of the map collection (each tile carries `coord_q`, `coord_r` properties).

---

## 5. State model

### 5.1 Scene-level (new) — `HexFinityMapProperties`

Lives at `bpy.types.Scene.hexfinity_map`.

```python
class HexFinityMapProperties(PropertyGroup):
    is_generated:     BoolProperty(default=False, options={'HIDDEN'})
    diameter_mm:      FloatProperty(default=100.0, min=0.001, soft_max=1000.0,
                                    update=_on_global_update)
    level_height_mm:  FloatProperty(default=5.0,   min=0.001, soft_max=100.0,
                                    update=_on_global_update)
    base_thickness_mm:FloatProperty(default=3.0,   min=0.001, soft_max=100.0,
                                    update=_on_global_update)
    subdivisions:     IntProperty  (default=4,     min=0,     soft_max=16,
                                    update=_on_global_update)
    grid_x:           IntProperty  (default=5,     min=0,     soft_max=64)
    grid_y:           IntProperty  (default=5,     min=0,     soft_max=64)
    root_collection:  PointerProperty(type=bpy.types.Collection)
```

Notes:
- `grid_x` / `grid_y` have **no** `update=` — they are read by the Regenerate operator only.
- The other four globals have `update=_on_global_update` which iterates every tile in the map collection, rebuilds its mesh, and (if `diameter_mm` changed) repositions each tile.
- `root_collection` is set when the map is generated. Used by the panel to detect "map exists" and by the regenerate operator to find what to delete.

### 5.2 Per-Object — `HexFinityProperties` (existing, modified)

```python
class HexFinityProperties(PropertyGroup):
    is_generated:   BoolProperty(default=False, options={'HIDDEN'})

    # REMOVED: diameter_mm, level_height_mm, base_thickness_mm, subdivisions
    #   → moved to the Scene-level group. These are MAP-WIDE INVARIANTS:
    #     every tile in the map shares the same value. Diameter in particular
    #     drives the grid pitch, so any per-tile divergence would tear the
    #     tessellation open. Per-tile authoring of these is intentionally
    #     impossible.

    # NEW:
    coord_q:        IntProperty(default=0, options={'HIDDEN'})
    coord_r:        IntProperty(default=0, options={'HIDDEN'})

    # UNCHANGED:
    p1..p6:         IntProperty(default=0, min=0, soft_max=20,
                                update=_on_corner_update)
    override_center:BoolProperty(default=False, update=_on_tile_local_update)
    center_level:   IntProperty(default=0, min=0, soft_max=20,
                                update=_on_tile_local_update)
    center_x_mm:    FloatProperty(default=0.0, update=_on_tile_local_update)
    center_y_mm:    FloatProperty(default=0.0, update=_on_tile_local_update)
```

The split between `_on_corner_update` (P1..P6: propagates to neighbours) and `_on_tile_local_update` (centre, override: tile-local only) keeps the propagation surface minimal.

---

## 6. Operators

### 6.1 `HEXFINITY_OT_generate_map`

`bl_idname = "hexfinity.generate_map"`, `bl_options = {'REGISTER', 'UNDO'}`.

Pre-condition: `scene.hexfinity_map.is_generated == False`.

Behaviour:
1. Read `grid_x`, `grid_y` from scene props.
2. If either is 0 → produce a single tile at `(q=0, r=0)`. Otherwise produce `grid_x * grid_y` tiles.
3. Create a collection named `"HexFinity Map"` under the scene's root collection.
4. For each `(q, r)`:
   - Create an empty mesh + Object named `f"HexTile_{q:02d}_{r:02d}"`.
   - Link to the map collection.
   - Set `obj.location = (*tile_world_xy(q, r), 0)`.
   - Set `obj.hexfinity_tile.coord_q = q`, `coord_r = r`.
   - Call `rebuild_tile(obj)` (see §6.4).
5. After all tiles built and `assert_two_manifold` passes for each:
   - `scene.hexfinity_map.root_collection = <created collection>`
   - `scene.hexfinity_map.is_generated = True`
6. Select the `(0, 0)` tile and make it active so the per-tile panel section appears.

On any failure during the loop: delete every tile created so far, delete the collection, leave `is_generated == False`, report error.

### 6.2 `HEXFINITY_OT_regenerate_map`

`bl_idname = "hexfinity.regenerate_map"`, `bl_options = {'REGISTER', 'UNDO'}`.

Uses `invoke_confirm` for the are-you-sure dialog (Blender built-in modal confirmation that shows operator name + a Yes/No):

```python
def invoke(self, context, event):
    return context.window_manager.invoke_confirm(self, event)

def execute(self, context):
    # 1. Delete every Object in scene.hexfinity_map.root_collection
    # 2. Delete the collection itself
    # 3. Clear scene.hexfinity_map.root_collection, set is_generated = False
    # 4. Call hexfinity.generate_map (or inline the same code path)
```

### 6.3 `HEXFINITY_OT_generate` (existing) — keep, re-purpose

Repointed to call into the map-generation path so the panel only needs one operator id for the prominent button. Or simply removed; the panel will surface the new ops directly. Decision: **remove the old single-tile op** to avoid two confusing entry points. The single-tile use case is preserved via `X == 0 or Y == 0`.

### 6.4 `rebuild_tile(obj)` (existing, modified)

Same signature/semantics as today, but it now reads the linear/subdivision params from `bpy.context.scene.hexfinity_map` instead of from `obj.hexfinity_tile`. Per-tile reads remain: `p1..p6`, `override_center`, `center_level`, `center_x_mm`, `center_y_mm`.

Adds **one new step**: `obj.location` is *not* changed here (only the mesh data is rebuilt — location is set at create time and by the global-update handler when `diameter_mm` changes).

---

## 7. Update callbacks & corner propagation

### 7.1 `_on_corner_update(self, context)`

Fires when a per-tile `P1..P6` is edited.

```python
_PROPAGATING = False   # module-level flag, separate from _REBUILDING

def _on_corner_update(self, context):
    global _PROPAGATING
    owner = self.id_data
    if not isinstance(owner, bpy.types.Object): return
    if not self.is_generated: return
    scene = context.scene
    if not scene.hexfinity_map.is_generated: return

    # Identify which corner changed by comparing values? Cleaner: register one
    # update callback per corner with the corner index baked in (functools.partial
    # can't be used directly with bpy props — generate six small closures or use
    # a small dispatch table by name).
    corner_idx = _which_corner_changed(self)   # 0..5
    new_value = (self.p1, self.p2, self.p3, self.p4, self.p5, self.p6)[corner_idx]

    if _PROPAGATING:
        # We are inside a propagating cascade — just rebuild self and return.
        rebuild_tile(owner)
        return

    _PROPAGATING = True
    try:
        affected = [owner]
        for (neighbour_dir, neighbour_corner_idx) in SHARED_CORNERS[corner_idx]:
            n_obj = _find_tile(scene, owner.hexfinity_tile.coord_q,
                                       owner.hexfinity_tile.coord_r,
                                       neighbour_dir)
            if n_obj is None: continue   # edge of map — no neighbour
            n_props = n_obj.hexfinity_tile
            setattr(n_props, f"p{neighbour_corner_idx+1}", new_value)
            affected.append(n_obj)
        for o in affected:
            rebuild_tile(o)
    finally:
        _PROPAGATING = False
```

Notes:
- The naive `setattr` write fires *that neighbour's* `_on_corner_update`, which sees `_PROPAGATING == True` and only rebuilds itself — which we already do explicitly afterwards. So the recursive call's rebuild is redundant. Cheapest fix: also short-circuit the rebuild inside the recursive call (we'll batch-rebuild after all writes). Concretely:

```python
if _PROPAGATING:
    return    # skip the in-recursion rebuild; the outer cascade rebuilds all
```

- "Which corner changed" detection: simplest is to generate six callbacks (`_on_p1_update`, …, `_on_p6_update`), each with the index baked in. Each calls into a shared `_corner_changed(self, idx)`. This is six trivial wrappers in `properties.py`.

### 7.2 `_on_tile_local_update(self, context)`

Same as the existing `_on_tile_prop_update` today — rebuilds *this* tile only. No propagation.

### 7.3 `_on_global_update(self, context)`

Fires on changes to `diameter_mm`, `level_height_mm`, `base_thickness_mm`, `subdivisions`.

```python
def _on_global_update(self, context):
    if not self.is_generated: return
    coll = self.root_collection
    if coll is None: return
    for obj in coll.objects:
        if not obj.hexfinity_tile.is_generated: continue
        rebuild_tile(obj)
        # If diameter changed, position depends on it — re-place the tile.
        obj.location = (*tile_world_xy(obj.hexfinity_tile.coord_q,
                                       obj.hexfinity_tile.coord_r,
                                       self.diameter_mm), 0.0)
```

(Diameter is read from scene props, so always pulling `self.diameter_mm` for the position call is safe regardless of which global actually changed — it just re-asserts the correct position.)

---

## 8. UI changes (`panel.py`)

State branches (in `draw`):

```
A) scene.hexfinity_map.is_generated == False
   --------------------------------------
   "HexFinity"
   ├─ Global parameters
   │   ├─ Diameter (mm)
   │   ├─ Level height (mm)
   │   ├─ Base thickness (mm)
   │   ├─ Subdivisions
   │   ├─ X (columns)
   │   └─ Y (rows)
   └─ [ Generate ]     ← hexfinity.generate_map

B) scene.hexfinity_map.is_generated == True
   --------------------------------------
   "HexFinity"
   ├─ Global parameters       (editable; cheap globals live-propagate;
   │   ├─ Diameter (mm)        X and Y only take effect on Regenerate)
   │   ├─ Level height (mm)
   │   ├─ Base thickness (mm)
   │   ├─ Subdivisions
   │   ├─ X (columns)
   │   └─ Y (rows)
   ├─ [ Regenerate ]   ← hexfinity.regenerate_map  (invoke_confirm)
   │
   └─ If active object is a HexFinity tile:
      ├─ "Editing: HexTile_qq_rr   (q=qq, r=rr)"
      ├─ Corner Levels (clockwise from upper-right)
      │   ├─ P1   [ int ≥ 0 ]
      │   ├─ P2   ...
      │   └─ P6   ...
      ├─ Center
      │   ├─ Override center level (toggle)
      │   ├─ Center level (int, enabled when override on)
      │   ├─ Center X (mm)
      │   └─ Center Y (mm)
      └─ (Subdivisions and the linear params are NOT shown here —
          they live in the global section above.)
```

Per-tile panel logic is largely the same as today minus the four params that moved to globals.

---

## 9. File-by-file change list

| File | Change |
|---|---|
| `hexfinity/properties.py` | Add `HexFinityMapProperties`. Modify `HexFinityProperties`: drop the four moved params; add `coord_q`, `coord_r`; split corner `update=` to a dedicated `_on_corner_update` per corner; rename existing `_on_tile_prop_update` → `_on_tile_local_update`. |
| `hexfinity/map.py` (NEW) | `SHARED_CORNERS` table; `tile_world_xy(q, r, diameter_mm)`; `neighbour_coord(q, r, direction)`; `find_tile(scene, q, r)`. Pure-Python so it's testable. |
| `hexfinity/operators.py` | Replace `HEXFINITY_OT_generate` with `HEXFINITY_OT_generate_map` and `HEXFINITY_OT_regenerate_map`. Adapt `rebuild_tile` to read globals from `scene.hexfinity_map`. Implement the propagation cascade (`_PROPAGATING` flag). |
| `hexfinity/panel.py` | Two-branch draw (no-map / map-exists). Show globals always; show per-tile section only when active object is a HexFinity tile. |
| `hexfinity/mesh_builder.py` | Two-line change for flat-top: corner-positions `angle` base shifts from `π/2` to `π/3`, and `clamp_center_to_hexagon`'s edge-normal `theta` base shifts from `π/3` to `π/6`. |
| `hexfinity/__init__.py` | Register `HexFinityMapProperties` and attach to `bpy.types.Scene.hexfinity_map`. Register two new operators. |
| `tests/test_map.py` (NEW) | Unit-test `tile_world_xy` (positions match expected pitches), `SHARED_CORNERS` (every shared corner is symmetric: A→B implies B→A on the inverse direction), `neighbour_coord` (odd-q vs even-q rules), and `find_tile`. |
| `tests/test_mesh_builder.py` | Update any tests that asserted P1 at `(0, +R)` to assert the new flat-top corner positions. |
| `README.md` | Add a new **"Terrain (X×Y map) generation"** section covering: the map invariants (uniform diameter / level height / base thickness / subdivisions), the X/Y semantics (counts, `0` ⇒ single tile), the odd-q offset layout, the shared-corner propagation behaviour, and the Regenerate-with-confirm flow. Also update the existing single-tile sections to reflect flat-top orientation. |
| `docs/hex_anatomy.svg` (UPDATE) | Rewrite the existing diagram to show the flat-top tile: P1..P6 re-labelled (P1 upper-right, P2 right, …, P6 upper-left), E1..E6 re-labelled accordingly, side view still showing base thickness and one level offset. |
| `docs/hex_map_anatomy.svg` (NEW) | New diagram showing a small assembled map (e.g. 3×3 odd-q offset) with: column / row indices q,r, the shared-corner vertices highlighted (the "three tiles meet at every interior vertex" property), and one example annotation showing that editing tile (q,r)'s P1 propagates to N's P3 and NE's P5. |

---

## 10. Implementation order (suggested PRs / commits)

1. **Geometry switch (small, self-contained).** Flip `mesh_builder.py` to flat-top. Update affected tests. Confirm a single tile still builds and is manifold.
2. **Map module + tests.** `hexfinity/map.py` with `SHARED_CORNERS`, coordinate math, neighbour lookup, plus its tests. No `bpy` deps so fully testable.
3. **Properties split.** Add `HexFinityMapProperties`. Move the four globals off the per-tile group. Add `coord_q` / `coord_r`. At this commit the panel will break — that's expected; the next commit fixes it.
4. **Operators + panel rewrite.** New generate-map / regenerate-map operators. New two-branch panel. Cascade logic. Verify in Blender on a 5×5 map: edit a P3, see the matching corners on the two neighbours change in lockstep, see a flat surface remain flat across the seam.
5. **Polish.** Edge-of-map propagation (no neighbour exists → skipped silently). Diameter-change re-layout (cheap globals propagate to every tile). Single-tile case (`X == 0 or Y == 0`).
6. **Docs.** Rewrite `docs/hex_anatomy.svg` for flat-top orientation; add `docs/hex_map_anatomy.svg` showing the assembled map with shared-corner highlights; add a new **"Terrain (X×Y map) generation"** section to `README.md` and update the single-tile sections to reflect flat-top. Done in this same effort, not deferred.

---

## 11. Open questions / explicit non-goals

**Open (worth deciding before step 4):**
- **`coord_q` / `coord_r` integrity.** Should we re-validate on every rebuild that the tile is still in the map collection at the position implied by its coords? Probably yes, as a soft sanity check — but no auto-repair.

**Resolved:**
- **Per-tile centre vs. shrinking `diameter_mm`.** Existing `clamp_center_to_hexagon` invoked from `rebuild_tile` (`hexfinity/operators.py:24-30`) suffices. `_on_global_update` (§7.3) runs `rebuild_tile` for every tile when `diameter_mm` changes, which re-clamps each tile's centre against the new diameter and writes the clamped values back to the per-tile props. The existing `_REBUILDING` re-entrancy flag protects the write-back. No map-mode-specific clamp logic is required because the centre is purely interior to a hex — it never reaches a shared corner.

**Explicit non-goals (this plan does not cover):**
- Non-rectangular map shapes (hexagonal map outline, custom masks).
- Adding/removing individual tiles after generation.
- Multiple maps per scene.
- Migration of any pre-existing `.blend` file that contains tiles created with the old (point-up, per-tile-diameter) version. The plugin is young and there are no users to break.
