# Hexfinity

Blender 5.1+ extension that generates a grid of interlocking hexagonal terrain tiles with per-corner height control.

## Ignore the ideas.md file. Only containing notes

## Run / build

- **Tests** (uses Blender's bundled Python; three modules below are bpy-free so they import in plain CPython):
  ```
  "C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pytest tests -v
  ```
- **Build / deploy**: `deploy.ps1` — zips to `dist/`, optionally junctions source into Blender's user extensions for live dev.
- **Extension manifest**: `blender_manifest.toml` (Blender 5.x format — not the legacy `bl_info` dict).

## Module map (`hexfinity/`)

| File | Responsibility |
|---|---|
| `__init__.py` | `register()` / `unregister()`; defers all bpy imports into `register()` |
| `properties.py` | `HexFinityMapProperties` (scene-level) + `HexFinityProperties` (per-tile P1–P6 corner heights) |
| `mesh_builder.py` | **bpy-free** — `build_hex_tile()`, six Coons patches on top, side walls, interlock tabs/holes |
| `map.py` | **bpy-free** — odd-q offset math, `SHARED_CORNERS` table, `neighbour_coord()`, `find_tile()` |
| `manifold_check.py` | **bpy-free** — `check_manifold()` validator run after every build |
| `operators.py` | `generate_map`, `regenerate_map`, `on_global_update` callback; `_REBUILDING` re-entrancy guard. `_build_map` seeds every new tile's P1–P6/center at `map_props.base_level` before the first build (corner callbacks short-circuit while `is_generated` is still `False`) |
| `panel.py` | N-panel "HexFinity" sidebar UI (two branches: pre-map and post-map) |
| `gizmo.py` | Floating-sphere gizmo for dragging a tile's center XY |
| `overlay.py` | P1–P6 corner labels drawn above selected tiles |
| `brush.py` | `HEXFINITY_OT_paint_brush` modal terrain brush — paints a per-top-vertex z-offset layer (`obj["hf_brush_disp"]`) re-applied by `build_hex_tile` on every rebuild |

## Invariants — preserve when editing

- **bpy-free rule**: `mesh_builder.py`, `map.py`, `manifold_check.py` must never import `bpy`. That isolation is what lets the pytest suite run against Blender's bundled Python without launching Blender.
- **Manifold guarantee**: every built tile is validated by `check_manifold()`; failure raises loudly so silent mesh corruption is caught.
- **Tab geometry is hardcoded** in `mesh_builder.py` (`TAB_WIDTH_MM`, `TAB_HEIGHT_MM`, `TAB_DEPTH_MM`, `TAB_HOLE_TOLERANCE_MM`) — these constrain minimum base thickness and diameter. Check the constants before changing related values.
- **Corner sync**: `SHARED_CORNERS` in `map.py` defines which neighbours share each corner; `on_global_update` propagates per-corner writes across seams.
- **Brush displacement layer**: top-surface verts are registered first in `build_hex_tile` (indices `0 .. num_top-1`), and `num_top` depends only on `smoothness_passes + resample_density` (see `top_vertex_count`, pinned to the builder by `test_top_vertex_count_matches_builder`). `obj["hf_brush_disp"]` is a `float[num_top]` z-offset layer applied (clamped to `base_thickness_mm`) inside the builder and re-sampled on every rebuild; `rebuild_tile` drops it when the length no longer matches `num_top` (a subdivision/resample change), which is the intended "paint survives height edits only" contract.

## Deeper docs

- `README.md` — geometry theory (Coons patches, G0 continuity), UI tree, diagrams, verification checklist.
- `terrain_creation_initial.md` — flat-top P1–P6 labelling decisions and kickoff design Q&A.


## Development requirements
* Always update the README documentation. Base level documentation update automatic. If needed subpages can be created/updated these are placed in the docs/ folder.
* write tests and validation when implementing.