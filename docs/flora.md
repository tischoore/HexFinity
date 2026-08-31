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
   `hf_brush_disp` layer are completely unaffected by planting or unplanting
   a tree. It never splits a rim edge, so the side wall's n-gon is untouched
   too.
4. Every vertex within `radius_mm` of a pad centre is then **lerped**
   (not additively offset) toward a height sampled from the surface *before*
   flattening, with a smoothstep falloff over `pad_blend_mm` and a
   rim-edge-distance fade so a pad near a hex edge shrinks rather
   than desyncing the seam with the neighbouring tile (the same fade
   terrain-object plateau pads use). Because it's a lerp,
   the pad interior is flat even where a procedural-surface texture or brush
   stroke would otherwise bump it — the pad simply overrides whatever was
   there.

**Penetration** still sinks the tree slightly into its now-flush pad
(default lowered from 2.0 mm to 0.3 mm since its old job — hiding a slope
gap — is superseded by the pad; its remaining job is avoiding a
zero-thickness, z-fighting contact between a perfectly flat tree base and a
perfectly flat pad).

## Pin/notch interlock (physical assembly)

A planted tree and its tile can be printed as two **separate** parts and
plugged together, the same idea as the tab/hole interlock between adjacent
hex tiles: a small cylindrical **pin** stands off the true base of the tree,
mating into a matching blind cylindrical **socket ("notch")** drilled into
the tile under the tree's flatten pad. All dimensions are hardcoded constants
in `mesh_builder.py`, deliberately independent of both a tree's own random
per-placement scale and the scene's `man_height_mm` print-scale slider:

| Constant | Value | Meaning |
|---|---|---|
| `FLORA_PIN_DIAMETER_MM` | 2.0 | Pin diameter — always exactly this, regardless of tree scale |
| `FLORA_PIN_HOLE_TOLERANCE_MM` | 0.2 | Socket grows by this over the pin, mirroring `TAB_HOLE_TOLERANCE_MM` |
| `FLORA_NOTCH_RADIUS_MM` | ~1.1 | Socket radius = pin radius + half the tolerance |
| `FLORA_NOTCH_DEPTH_MM` | 10.0 | Socket depth |
| `FLORA_PIN_LENGTH_MM` | 9.8 | Pin length = socket depth − tolerance, so the tip never bottoms out before the tree's base seats flush |

**Cost control — cut only on finalize.** Unlike the flatten pad (recomputed
on every `rebuild_tile`), drilling a real socket is expensive enough that it
must not run on the interactive per-click rebuilds that already fire while
placing trees, nor on any other rebuild trigger (brush stroke, corner-height
edit, terrain snap). `operators.rebuild_tile(obj, finalize_flora=False)`
gates it: the default `False` (every existing call site) skips
`flora.notch_specs`/`tree_pads.cut_notches` and any pin objects entirely;
`finalize_flora=True` — only reached from `HEXFINITY_OT_flora_marker._finish`
(leaving the planting tool) or the manual **Finalize Flora** button — cuts
the socket and (re)creates the pin. This means pins/notches only exist right
after a finalize pass; any later edit (even an unrelated one, like painting
elsewhere with the brush) strips them again until Finalize Flora runs once
more. The panel's Flora box explains this with an inline note next to the
button.

**The socket cut (`tree_pads.cut_notches`, bpy-free)** runs strictly after
the pad flatten, so it always cuts into a surface already known to be flat.
It forces its own deeper local refinement (`NOTCH_MAX_LEVELS = 8` — the
~1.1 mm notch radius is far smaller than a typical pad, needing much finer
edges than the pad's own flatten pass produces), then removes every triangle
fully inside the notch radius, walks the resulting hole's boundary into an
ordered loop, snaps that loop onto an exact circle (so a real printed pin
fits), and builds a cylindrical wall + floor down to the socket depth —
reusing the removed region's own (already correctly wound) triangulation for
the floor rather than a fresh fan, so no vertex is ever left orphaned. A
notch that can't be safely cut — the local mesh is too coarse even after
forced refinement, its boundary is a pinch point or several disjoint loops,
it reaches the hex rim, or the tile is too thin for the requested depth — is
silently skipped (left un-drilled) with a logged warning rather than risking
a corrupt or non-manifold mesh.

**The pin object (`flora._get_or_build_pin_mesh`)** is a plain procedural
cylinder built once per session via `from_pydata` — **not** baked into the
shared species STL mesh, since that mesh is linked across every instance of
a species and gets non-uniformly scaled per placement (`obj.scale`); baking
the pin into it would make its real-world size drift with each tree's random
`scale_factor`. Instead it's its own shared mesh, instanced as a **child of
the tree it belongs to** (not the tile) — it moves as one unit with its
tree, and is found via the tree's own `.children` (see `flora.sync_flora`).
Its own `obj.scale` is set to `1 / total_scale`, exactly cancelling the
tree's `obj.scale = (total_scale,)*3`, so the pin's real-world size stays
exactly `FLORA_PIN_DIAMETER_MM`/`FLORA_PIN_LENGTH_MM` regardless of the
tree's own random scale. Its local position is
`(0, 0, min_z + penetration_mm / total_scale)` in the tree's own frame —
not just `(0, 0, min_z)` — so the pin's top always lands exactly at the
resolved surface height *regardless of `penetration_mm`*: anchoring it at
the tree's own (already-sunk) local origin instead would let the pin's tip
poke past the socket floor once `penetration_mm` exceeds
`FLORA_PIN_HOLE_TOLERANCE_MM` (already true at the defaults, 0.3mm vs
0.2mm). `sync_flora` only attaches a pin for a placement index that
`cut_notches` actually succeeded on (`ok_indices`), so a partial failure —
one tree's socket skipped, others fine — can never leave a pin floating over
an undrilled spot; `purge_flora` removes a tree's pin before the tree itself
so nothing is orphaned.

**Seating uses the known pad height, not a raycast into the hole.**
Once a socket is cut, a straight-down raycast at the placement's exact
`(x, y)` — the notch's own centre — would pass through the ~1.1mm-wide
opening and hit the socket floor, ~`FLORA_NOTCH_DEPTH_MM` below the real
surface, instead of the surrounding pad. `tree_pads.cut_notches` already
knows the pad's exact pre-drill flat height (`pad_z`) for every notch it
cuts, so it reports it back via an optional `resolved_heights` dict
(`{index: pad_z}`), threaded through `build_hex_tile`'s
`flora_notch_heights` kwarg and `operators.rebuild_tile`. `flora.sync_flora`
uses `notch_heights[i]` directly when present instead of raycasting — the
raycast fallback only ever runs for a non-finalized rebuild or a placement
whose notch was skipped, where there's no hole to worry about.

**Export.** `HEXFINITY_OT_export_tiles` exports a planted tree and its pin
merged into **one** STL file — `hex_qNN_rNN_treeII.stl`
(`tile_export.flora_placement_filename`) — sharing the tile's own
`hex_qNN_rNN` naming stem so all of a tile's files sort together in a file
browser, listed in `flora_manifest.csv`/`.json` (one `file` column per
placement). Unlike tile STLs, flora files are never deduped by content
hash — every placement always gets its own file, named for its own
tile+index. `_terrain_children` excludes flora tree/pin objects from the
tile's fused export (scatter boulders and terrain-import objects are
unaffected); the notch cavity itself needs no separate handling since it's
already baked into the tile's own mesh.

Since the pin is parented to the tree with a fixed local
offset/rotation/scale, `_export_flora_pair` moves and rotates only the tree
— the pin is carried along rigidly, unchanged relative to it — treating the
pair as one rigid body for orientation purposes, then selects both objects
and writes them with a single `wm.stl_export` call. STL has no object
concept (the same trick `_export_objects` uses to fuse a tile with its
terrain children), so the two meshes merge into one triangle soup in the
file — two disjoint shells, no boolean/topology fusion, printable exactly as
they were as two separate files. As-authored (tree upright, pin hanging from
the tree's base down into the socket), the pin's tip is always the
assembly's lowest point, the worst possible print-bed contact for a thin
2mm peg. The tree is rotated 180° about its own local X axis (always a
horizontal axis regardless of the tree's random per-placement yaw, since yaw
only rotates about world Z) before export, flipping the whole rigid body
upside down: the canopy tip — the assembly's original highest point —
becomes the new lowest point (touching the print bed), and the pin —
previously lowest — becomes the new highest point, pointing up. This
canopy-down orientation was confirmed as intended rather than re-seating the
tree on its own base independently of the pin. Each part's own lowest point
after the flip is computed via the same evaluated-mesh machinery used
elsewhere (`_eval_mesh_local`), and the combined lowest of the two is
shifted to world z=0. A tile with planted trees but no matching pins (never
finalized) triggers a `{'WARNING'}` at export time pointing at Finalize
Flora, rather than silently exporting trees with no pins and a tile with no
sockets.

## Scene tree

```
HexFinity Map (collection)
├─ HexTile_00_00              (the tile object)
└─ Flora (collection)
   ├─ Flora_HexTile_00_00_000     (parented under the tile; linked mesh)
   │  └─ FloraPin_HexTile_00_00_000  (parented under ITS TREE, not the tile;
   │                                    only present right after a Finalize
   │                                    Flora pass)
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
7. **Export** — *Export Tiles to STL* excludes planted trees from the tile's
   own STL (scatter boulders and terrain objects are still fused in as
   before); each finalized tree exports as one merged STL
   (`hex_qNN_rNN_treeII.stl`, tree + pin together), listed in
   `flora_manifest.csv`. A tile with unfinalized flora triggers a warning
   instead of silently exporting mismatched parts.
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
11. **Pin/notch interlock** — plant a tree, press Esc/RMB to leave the Flora
    tool: confirm a socket visibly appears under the tree (e.g. toggle the
    tree's visibility), the tree still sits flush on the surface (not sunk
    into the socket), and a `FloraPin_*` object appears **nested under its
    tree** in the Outliner (not as a separate top-level sibling). Paint a
    brush stroke elsewhere on the tile (or edit a corner height) and confirm
    the pin disappears and the socket fills back in — then press
    **Finalize Flora** and confirm both come back, tree still flush. Export
    the tile: confirm one merged tree+pin STL is written alongside the
    tile's own STL — `hex_qNN_rNN_treeII.stl`, containing both shells —
    that `flora_manifest.csv` lists it, and that the combined lowest point
    of the pair sits at z=0 with the pin (now flipped to point up) the
    highest feature of the two. Plant a tree, do *not* finalize, and export:
    confirm a warning appears and no tree/pin STL is written for that
    placement. A headless smoke test of this whole path (plant → finalize →
    pin/socket geometry → seating correctness → un-finalize → re-finalize →
    export) lives in `tests/_headless_flora_pin_check.py`.
