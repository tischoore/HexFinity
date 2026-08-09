# Hexfinity

Blender 5.1+ extension that generates a grid of interlocking hexagonal terrain tiles with per-corner height control.

## Ignore the ideas.md file. Only containing notes

## Run / build

- **Tests** (uses Blender's bundled Python; six modules below — `mesh_builder.py`, `map.py`, `manifold_check.py`, `procedural_surfaces.py`, `subdivision.py`, `tile_export.py` — are bpy-free so they import in plain CPython). Blender's bundled interpreter doesn't ship pytest, so install it once first:
  ```
  "C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pip install --user pytest
  "C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pytest tests -v
  ```
- **Build / deploy**: `deploy.ps1` — zips to `dist/`, optionally junctions source into Blender's user extensions for live dev.
- **Extension manifest**: `blender_manifest.toml` (Blender 5.x format — not the legacy `bl_info` dict).

## Module map (`hexfinity/`)

| File | Responsibility |
|---|---|
| `__init__.py` | `register()` / `unregister()`; defers all bpy imports into `register()` |
| `properties.py` | `HexFinityMapProperties` (scene-level) + `HexFinityProperties` (per-tile P1–P6 corner heights), plus `HexFinityBrushProperties` (brush radius/strength/direction/preserve_edge), `HexFinitySurfacePoint`/`HexFinitySurfaceRegion` (procedural-surface region authoring), and `HexFinityTerrainProperties` (`snap_mm`/`snap_damp_mm`/`snap_tile` driving `apply_terrain_snap`) |
| `mesh_builder.py` | **bpy-free** — `build_hex_tile()`; top surface is Loop subdivision (via `subdivision.py`) over a 13-vertex control mesh (`C`, `P1..P6`, `Q1..Q6`) with sharp-tagged rim edges — supersedes the earlier Coons-patch construction; side walls; interlock tabs/holes |
| `map.py` | **bpy-free** — odd-q offset math, `SHARED_CORNERS` table, `neighbour_coord()`, `find_tile()`, `tile_world_xy()`, `clamp_level()`/`apply_corner_delta()` |
| `manifold_check.py` | **bpy-free** — `assert_two_manifold(verts, faces)` validator run after every build |
| `subdivision.py` | **bpy-free** — `subdivide_loop()` (Loop subdivision with sharp-edge/crease support) and `linear_midpoint_subdivide()`; imported by `mesh_builder.py` for the top surface |
| `tile_export.py` | **bpy-free** — `tile_geometry_hash`, `short_hash`, `is_custom_tile`, `tile_filename`, `manifest_rows`: dedup/naming/manifest helpers consumed by `operators.HEXFINITY_OT_export_tiles` |
| `operators.py` | `generate_map`, `clear_map` (destructive-only — deletes the whole map collection + all tiles/terrain/scatter, resets `is_generated`/`show_globals`; does **not** rebuild), `on_global_update` callback; `_REBUILDING` re-entrancy guard. `_build_map` seeds every new tile's P1–P6/center at `map_props.base_level` before the first build (corner callbacks short-circuit while `is_generated` is still `False`). `on_corner_changed` also fans a corner edit's **delta** across a multi-selection: `_MULTI_APPLYING` guards the fan-out, and `_ACTIVE_SNAPSHOT`/`seed_corner_snapshot*` (seeded from `panel.py`) recover the pre-edit value Blender's update hook hides. Also: `HEXFINITY_OT_import_terrain_object`/`HEXFINITY_OT_redrop_terrain_object` (STL terrain-object placement/parenting), the terrain-snap-to-model subsystem (`apply_terrain_snap`, `_compute_snap_gap`, `_snap_signature`, `_reset_tile_snap`), and `HEXFINITY_OT_export_tiles` (per-tile STL export + dedup + manifest writing, via `tile_export.py`) |
| `panel.py` | N-panel "HexFinity" sidebar UI (two branches: pre-map shows editable globals+grid+Generate; post-map shows the `clear_map` button + a collapsed read-only "Map Settings" section gated by `map_props.show_globals`, via `_draw_globals`/`_draw_grid`). Also: `_draw_tile_section` (corner sliders, center controls, terrain-object import), `_draw_surface_regions`/`_draw_scatter_params` (procedural-surface/scatter region UI incl. vertex-budget warning), the Terrain Brush UI block, and the Export box (`hexfinity.export_tiles`) |
| `gizmo.py` | Floating-sphere gizmo for dragging a tile's center XY |
| `overlay.py` | P1–P6 corner labels drawn above selected tiles; `_draw_regions` draws each selected tile's procedural-surface region polygons plus a direction arrow for anisotropic (furrow) surfaces |
| `brush.py` | `HEXFINITY_OT_paint_brush` modal terrain brush — paints a per-top-vertex z-offset layer (`obj["hf_brush_disp"]`) re-applied by `build_hex_tile` on every rebuild |
| `procedural_surfaces.py` | **bpy-free** — `SURFACES` registry (single source of truth). Two surface *kinds*: `displace` (`generator(x,y,…)->z`, masked + faded into the tile top) and `scatter` (`generator=None`, carries `placement_fn`+`element_mesh_fn`+`extra_params` of `ParamSpec`). Scatter geometry `_icosphere`/`boulder_mesh`/`scatter_boulders`/`assemble_scatter_mesh`; `hex_polygon`, `estimate_boulder_count` |
| `regions.py` | bpy region authoring (imports `bpy` directly — only its pure helpers are bpy-free, and it has no automated test coverage; exercised via the manual `tests/_headless_region_check.py` checklist) — modal `HEXFINITY_OT_draw_region` point picker + add/remove operators + `HEXFINITY_UL_surface_regions` list |
| `scatter.py` | bpy shell for `scatter` surfaces — `sync_scatter` (placement→raycast-down Z seating→assemble→one joined `Boulders_<AreaName>` object parented under the tile, tagged `obj["hf_scatter_of"]`), `purge_scatter`, `HEXFINITY_OT_merge_scatter` (boolean-union into the tile for printing). Called from `operators.rebuild_tile` inside `_REBUILDING`, after the new mesh is assigned + depsgraph updated |

## Invariants — preserve when editing

- **bpy-free rule**: `mesh_builder.py`, `map.py`, `manifold_check.py`, `procedural_surfaces.py` (incl. all scatter geometry math), `subdivision.py`, and `tile_export.py` must never import `bpy`. That isolation is what lets the pytest suite run against Blender's bundled Python without launching Blender. `regions.py`/`scatter.py` themselves do import `bpy` — keep object/raycast/boolean code there, and keep any pure-math helpers extracted into it bpy-free.
- **Manifold guarantee**: every built tile is validated by `assert_two_manifold()`; failure raises loudly so silent mesh corruption is caught.
- **Tab geometry is hardcoded** in `mesh_builder.py` (`TAB_WIDTH_MM`, `TAB_HEIGHT_MM`, `TAB_DEPTH_MM`, `TAB_HOLE_TOLERANCE_MM`) — these constrain minimum base thickness and diameter. Check the constants before changing related values.
- **Corner sync**: `SHARED_CORNERS` in `map.py` defines which neighbours share each corner; `on_global_update` propagates per-corner writes across seams.
- **Scatter kind contract**: a `scatter` `Surface` has `generator=None`, so `surface_offset()` returns `0.0` (zero displacement) and the `GENERATING` test fan-out (`s.generator is not None`) excludes it — do not give a scatter surface a generator. Scatter objects are regenerated by `scatter.purge_scatter` + `sync_scatter` on every `rebuild_tile`; the seating raycast must run *after* the tile mesh is assigned and the depsgraph updated so it samples current (not stale) geometry.
- **Brush displacement layer**: top-surface verts are registered first in `build_hex_tile` (indices `0 .. num_top-1`), and `num_top` depends only on `smoothness_passes + resample_density` (see `top_vertex_count`, pinned to the builder by `test_top_vertex_count_matches_builder`). `obj["hf_brush_disp"]` is a `float[num_top]` z-offset layer applied (clamped to `base_thickness_mm`) inside the builder and re-sampled on every rebuild; `rebuild_tile` drops it when the length no longer matches `num_top` (a subdivision/resample change), which is the intended "paint survives height edits only" contract.

## Deeper docs

- `README.md` — geometry theory (Loop subdivision over the 13-vertex control mesh, sharp-tagged rim edges), UI tree, diagrams, verification checklist.


## Development requirements
* Always update the README documentation. Base level documentation update automatic. If needed subpages can be created/updated these are placed in the docs/ folder.
* write tests and validation when implementing.

## PR Plans

Plan lifecycle is driven by two trigger phrases in my prompts:

**Start:** `plan <name>.md: <prompt>`
→ Create `plans/brewing/<name>.md` containing the prompt and the proposed plan. Work proceeds against this file.

**End:** `end plan`
→ Move the active plan from `plans/brewing/` to `plans/implemented/` and append the Token Usage Report.

### Token Usage Report (appended on `end plan`)
Counts come from the API `usage` object on each response — never self-estimate.
Aggregate across all requests made since the matching `plan` trigger:

- Input tokens (prompt): sum of `usage.input_tokens`
- Cache tokens: `cache_creation_input_tokens` + `cache_read_input_tokens`
- Output tokens (answer): sum of `usage.output_tokens`
- MCP / tool-call tokens: input+output of requests issuing tool_use/tool_result
- Web request tokens: input+output of requests invoking web tools
- Source-search tokens: input+output of requests doing code/file search
- Project files read: count of distinct files opened
- Total tokens: input + output + cache