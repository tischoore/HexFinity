# Tree-Planting Brush (removable, plug-in trees)

## Context

The user wants to "paint" trees onto hex tiles from a modal brush, the same
way `hexfinity.paint_brush` paints terrain height today. Trees come from a
user-supplied folder of mesh files (STL) plus a JSON manifest describing each
resource, including a reference real-world height used to scale the mesh
against the plugin's existing man-height reference (`man_height_mm`, already
used by `procedural_surfaces.ParamSpec.scaled_default`). Each tree carries a
cylindrical peg at its base; the plugin must cut a matching socket into the
tile's top surface (constant `0.02mm` clearance) so the tree is a separate,
removable, printed part rather than a body permanently fused to the tile.

The open technical question was **how to cut that socket without a boolean
operator** — the codebase already avoids booleans for the inter-tile
tab/hole interlock (`mesh_builder.py`'s `TAB_*` constants), building cavities
by hand-constructing vertices/faces so the result is watertight *by
construction* rather than by CSG subtraction, and the user asked whether the
same philosophy generalizes here. Investigation confirmed it does, with one
real tradeoff: the top surface is an irregular Loop-subdivided triangle mesh
(no coordinate grid), so a socket must be centered on an *existing mesh
vertex*, not a literal arbitrary (x,y) — meaning tree placement snaps to the
nearest eligible vertex within brush radius rather than being truly
continuous. At normal subdivision density this is visually indistinguishable
from freeform painting. The user confirmed this tradeoff and also confirmed
a single global peg size (one constant diameter/height/tolerance for every
tree species, mirroring the existing `TAB_WIDTH_MM`-style constants) rather
than per-tree peg dimensions.

Outcome of this investigation: **the feature is feasible**, no boolean is
required, and the plan below reuses three existing subsystems almost
directly — the terrain brush's modal/raycast/stroke-accumulation shape, the
scatter system's purge/resync-on-rebuild lifecycle for discrete child
objects, and the STL-import operator's `bpy.ops.wm.stl_import` +
world-transform-preserving parenting idiom. Per-tile STL export needs **no
new code**: `HEXFINITY_OT_export_tiles` already bundles every mesh child of
a tile (`_mesh_children`, type-based, not tag-based) into one multi-body STL,
which is exactly what "separate, removable, printable" trees need — multiple
disjoint bodies in one STL print as separate physical parts.

## Core technique: vertex-fan socket carve (no boolean)

In `mesh_builder.py`, after the top surface is subdivided and
`top_displacement` (brush/snap layers) is baked in, but before any
bottom-plate/tab/hole/side-wall geometry is appended:

1. Pick an **interior** top-surface vertex `V` (index `< num_top`, no
   incident sharp/rim edge) as the socket center.
2. Walk `V`'s incident triangle fan to get its ordered ring of neighbour
   vertices `N_0..N_{k-1}` (`k` = valence, typically 6 near the centre but
   **not guaranteed** — must handle arbitrary valence, including the
   non-6 vertices that persist near the original 13-vertex control points at
   low subdivision).
3. Remove the `k` fan triangles. `N_0..N_{k-1}` are kept **unchanged** — they
   stay exactly where they are, still connected to the rest of the mesh.
4. Add a new inset ring `I_0..I_{k-1}` at socket radius (peg radius +
   `0.02mm`) from `V`, each point interpolated in 3D along the segment
   `V → N_i` (follows local slope, not flat XY).
5. Add a second inset ring `I'_0..I'_{k-1}` at the same XY as `I` but at the
   socket floor Z (`V.z - hole_depth_mm`).
6. Sink `V` itself in place (same index, no renumbering) to the floor Z and
   reuse it as the floor's fan apex — this avoids orphaning `V` (which the
   manifold checker would reject) and keeps every existing index stable, so
   `top_vertex_count()` / the `hf_brush_disp` displacement-layer contract is
   completely undisturbed.
7. New faces: `k` annulus quads (`N_i, N_{i+1}, I_{i+1}, I_i`) replacing the
   deleted fan, `k` wall quads (`I_i, I_{i+1}, I'_{i+1}, I'_i`), `k` floor
   triangles (`V, I'_i, I'_{i+1}`).
8. New vertices are appended strictly after index `num_top - 1`, so they
   never collide with the top-displacement index range.

Every edge in the new geometry is shared by exactly two faces and every
vertex stays referenced, so `manifold_check.assert_two_manifold()` — already
run on every build — validates the result unchanged, with no new special
casing.

**Hard rejection rules** (checked before carving, and re-checked as the
carve's own final authority): candidate too close to the sharp rim
(`rim_edge_distance()`, already used by the terrain brush's `preserve_edge`);
fewer than 3 incident triangles or a non-closing fan walk (corrupt/boundary
vertex); center or ring vertex already claimed by another socket in the same
build (no overlapping fans); local edge length too short relative to the
socket radius (degenerate/self-intersecting geometry — real risk at low
`smoothness_passes`, where non-6 valence and sharper local curvature both
concentrate); floor Z too close to the tile's bottom plane. A placement that
fails is skipped (not a hard build failure) — the brush's own pre-flight
check should usually keep this from happening interactively, but slope /
subsequent edits can still invalidate a previously-good placement.

## Files to add/change

| File | Change |
|---|---|
| `mesh_builder.py` | New constants `TREE_PEG_DIAMETER_MM`, `TREE_PEG_HEIGHT_MM`, `TREE_HOLE_TOLERANCE_MM = 0.02` (mirrors `TAB_*` style). New pure functions: `_vertex_ring()` (arbitrary-valence fan walk), `is_valid_tree_socket_center()` (shared probe used both by the interactive brush and by the carve itself), `carve_tree_socket()` (the algorithm above, raises a new `TreeSocketError` on failure), `nearest_top_vertex()` (re-resolution helper for topology changes). `build_hex_tile()` gains optional `tree_sockets=` (list of `{"vertex_index": int}`) and an out-param `tree_socket_results=` (accepted/rejected + each accepted socket's pre-carve apex XYZ) — added as new keyword args so all ~30 existing 2-tuple call sites are untouched. |
| `manifold_check.py` | No change — existing `assert_two_manifold()` already covers the new geometry's invariants. |
| `procedural_surfaces.py` | Small new helper `model_scale_factor(man_height_mm)` (`man_height_mm / REAL_MAN_HEIGHT_MM`) — the same ratio `ParamSpec.scaled_default` already applies to feature sizes, extracted so tree-mesh scaling reuses it directly. |
| `tree_library.py` (new, bpy-free) | `parse_manifest(json_text)` — validates the tree manifest schema (`id`, `name`, `file`, `reference_height_mm`; rejects duplicate ids / non-positive heights), raising `TreeManifestError`. Kept separate from mesh geometry so it's unit-testable without Blender, same rationale as `tile_export.py`. |
| `trees.py` (new, bpy) | Mirrors `scatter.py`'s shape: `purge_trees`/`sync_trees` lifecycle for tree child objects (tagged `hf_tree_of`/`hf_tree_species`, parented to the tile, linked-duplicate mesh data shared across placements of the same species); `ensure_species_cached()` (lazy `bpy.ops.wm.stl_import`, same guarded/try-except pattern as `HEXFINITY_OT_import_terrain_object`, scale baked in once via `transform_apply`); `rescan_tree_library()` + `HEXFINITY_OT_rescan_tree_library` / `HEXFINITY_OT_reimport_tree_species` operators; `HEXFINITY_UL_tree_species` UIList. |
| `tree_brush.py` (new, bpy) | `HEXFINITY_OT_paint_tree_brush` — modal operator structurally modeled on `brush.py`: same draw-handler/raycast/candidate-tile-cull/stroke-accumulate-then-commit shape, but instead of a per-vertex float displacement it accumulates *discrete placements* (nearest eligible top-surface vertex within radius, density-gated via the existing `procedural_surfaces._hash01` deterministic-hash pattern so one drag doesn't spam every vertex, `min_spacing_mm` exclusion) or *removals* in ADD/REMOVE mode. STL import (`ensure_species_cached`) only ever happens here, on stroke-end, in a guaranteed interactive context — never inside `rebuild_tile`. |
| `properties.py` | `HexFinityTreeSpecies`/`HexFinityTreeLibraryProperties` (scene-level: folder path, parsed species list, active index) — independent of `HexFinityMapProperties` since a library can be configured before a map exists. `HexFinityTreePlacement` (per-tile `CollectionProperty`: `vertex_index` + continuous `local_x_mm`/`local_y_mm` fallback + `species_id` + `rotation_rad`) added to `HexFinityProperties`. `HexFinityTreeBrushProperties` (scene-level: `radius_mm`, `density`, `min_spacing_mm`, ADD/REMOVE `mode`). |
| `operators.py` | `rebuild_tile()` gains a block (after the existing brush/snap displacement resolution, before `build_hex_tile()`) that: re-resolves `vertex_index` via `nearest_top_vertex()` when a topology change is detected (`num_top` mismatch against a cached `hf_tree_topology_ntop`) instead of dropping placements outright — a better contract than the brush layer's "changing subdivision clears your paint," made possible because placements also carry continuous XY; builds `tree_sockets`/`tree_socket_results`; passes them into `build_hex_tile()`; and, after mesh assignment + the existing `view_layer.update()` flush (same ordering scatter already relies on), calls `trees.sync_trees()`/`purge_trees()`. `_mesh_children`/`HEXFINITY_OT_export_tiles` need no changes — trees are picked up automatically. |
| `panel.py` | New scene-wide "Tree Library" box (folder path, Rescan button, species list, per-species Reimport + a staleness warning if `man_height_mm` changed since that species was cached — scale is baked in once at first paint and does **not** auto-track later man-height edits, an explicit documented limitation). New "Tree Brush" box in `_draw_tile_section`, right after the existing "Terrain Brush" box (mode toggle, radius/density/min-spacing sliders, Paint button), gated on at least one species being loaded. |

## Verification

- `tests/test_tree_socket.py` (bpy-free, plain CPython like the rest of the
  suite): `_vertex_ring` at multiple valences (5/6/7, including vertices
  pulled from a real low-`smoothness_passes` `build_hex_tile()` output where
  non-6 valence is common); `carve_tree_socket` → `assert_two_manifold`
  end-to-end; rejection cases (rim-adjacent, overlapping socket, degenerate
  edge length); `build_hex_tile(tree_sockets=..., tree_socket_results=...)`
  accept/reject reporting and `apex_xyz` correctness; `nearest_top_vertex`.
- `tests/test_tree_library.py` (bpy-free): manifest parsing — valid case,
  missing key, duplicate id, non-positive reference height.
- `tests/test_procedural_surfaces.py`: trivial `model_scale_factor` ratio
  check.
- Run via the existing Blender-Python pytest invocation from `CLAUDE.md`.
- Manual, in-Blender checklist (new `docs/tree-brush.md`, mirroring
  `docs/boulder-field.md`): point the library at a real folder + manifest,
  paint a stroke, confirm sockets appear only where expected and trees sit
  correctly seated; drag ADD across an already-populated area to confirm
  `min_spacing_mm`/density behave; switch to REMOVE and erase; bump
  `smoothness_passes`/`resample_density` and confirm placements re-snap
  (not vanish); export tiles and confirm the multi-body STL contains
  separate tile + tree bodies with pegs visually matching their sockets.
