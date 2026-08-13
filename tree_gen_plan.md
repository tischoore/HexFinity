# Trees scatter surface

## Context

The Instructables article "Procedurally Generated Trees" describes the standard recursive/branching technique for generating tree meshes (a trunk that splits into child branches with randomized angle/length, topped with foliage). We asked whether that idea could populate HexFinity's hex tiles with forests, and traced it against the existing "Boulder Field" scatter system (`hexfinity/procedural_surfaces.py`, `scatter.py`, `panel.py`), which already scatters procedurally-varied objects onto tiles via a `Surface(kind='scatter', ...)` registry entry with a `placement_fn` + `element_mesh_fn`.

User decision: build **single-level branching** trees — one trunk that splits into 3-6 randomized limbs, each tipped with a foliage cluster — rather than a plain trunk+canopy "lollipop" or full recursive/L-system branching. This is complex enough to look tree-like but avoids the much harder nested-branch-junction manifold problem.

The crux finding that makes this tractable: `manifold_check.assert_two_manifold()` only checks per-edge face-multiplicity (==2) and orphan vertices — it has **no self-intersection or connectivity check**. The existing boulder pipeline already exploits this by concatenating multiple closed, non-vertex-sharing icospheres across placements. A tree instance can use the exact same trick *within* one instance: trunk, limbs, and canopies are each built as independently-closed 2-manifold solids that generously overlap their parent piece (limb base sunk into the trunk volume, canopy pulled back over the limb tip) — no vertex welding, no bpy boolean needed at mesh-generation time. This keeps `tree_mesh` bpy-free like `boulder_mesh`.

## Implementation

### 1. `hexfinity/procedural_surfaces.py` (bpy-free)

- Add small transform helpers: `_translate`, `_rotate_y`, `_rotate_z`, `_append_piece` (concatenate with vertex-index offset).
- Add `_capped_cylinder(radius_bottom, radius_top, height, segments)` — closed, 2-manifold frustum along +Z (base ring at z=0, top ring at z=height, both ends triangle-fan-capped). Cone is the `radius_top≈0` special case. Sibling to `_icosahedron`/`_icosphere`.
- Refactor `boulder_mesh`'s per-vertex radial-noise loop into a shared `_roughen(unit_verts, radius_mm, pid, roughness, seed_tag)` helper (pure refactor — `boulder_mesh` becomes `_icosphere(subdiv)` + `_roughen(..., seed_tag=<boulder constant>)`, same output). Add trailing `**_` to `boulder_mesh`'s signature.
- Add `tree_mesh(height_mm, pid, *, trunk_radius_mm=6.0, min_limbs=3, max_limbs=6, canopy_radius_factor=0.22, trunk_split_frac=0.55, trunk_taper=0.6, limb_taper=0.7, roughness=0.3, subdiv=0, trunk_segments=8, limb_segments=6, **_) -> (verts, faces)`:
  - Trunk: `_capped_cylinder` from z=0 to `trunk_split_frac*height_mm`.
  - Limb count `n ∈ [min_limbs, max_limbs]` hashed from `pid` via `_hash01` (same determinism pattern as boulder roughness).
  - Per limb `i`: hash-derived azimuth (evenly spread + jitter), tilt (~28°-63° from vertical), length, and taper; build as a local `_capped_cylinder` along +Z, rotate (`_rotate_y` then `_rotate_z`), translate so its base sits well inside the trunk's solid (not just touching its surface).
  - Canopy: `_icosphere(subdiv)` roughened via `_roughen` (own seed tag, salted per-limb), centered at the limb tip but pulled back along the limb axis so it swallows the tip.
  - Concatenate all pieces via `_append_piece`; return combined `(verts, faces)`.
  - Factor "how many limbs for this pid" into a small testable helper, e.g. `_tree_limb_count(pid, min_limbs, max_limbs)`.
- Add `scatter_trees(polygon, *, min_height_mm=1500.0, max_height_mm=5000.0, density=0.4, seed=0, **_)` returning the same 5-tuple shape as `scatter_boulders` (`x_mm, y_mm, height_mm, rot_rad, pid`). Extract the shared jittered-grid placement core out of `scatter_boulders` into a private `_jittered_grid_placements(polygon, *, pitch, presence, size_fn, seed)` helper reused by both (boulders keep their `distribution`-shaped power-curve size closure; trees use a plain linear lerp between min/max height and a wider pitch derived from a `TREE_CANOPY_FOOTPRINT_FRAC = 0.5` constant, since trees need sparser spacing relative to size than boulders).
- Add `estimate_tree_count(polygon, *, min_height_mm, max_height_mm, density)` (mirrors `estimate_boulder_count`) and `tree_typical_vertex_count(min_limbs=3, max_limbs=6, subdiv=0, trunk_segments=8, limb_segments=6, **_)` (sums trunk + avg-limb-count × (limb + canopy) vertex counts) for the panel's vertex-budget warning.
- Generalize `Surface.__init__` with new **optional, backward-compatible** kwargs: `seat_offset_fn=None`, `element_kwargs=None`, `sink_mm=1.0`, `scatter_prefix=None`, `count_estimate_fn=None`, `typical_vertex_count_fn=None`. `BOULDERS`'s entry gains explicit values for these (`element_kwargs={"roughness": 0.4, "subdiv": 1}`, `seat_offset_fn=lambda size: size`, `sink_mm=1.0`, `scatter_prefix="Boulders"`, `count_estimate_fn`/`typical_vertex_count_fn` wrapping the existing boulder estimators) so its behavior is unchanged, just now data-driven.
- Generalize `assemble_scatter_mesh(placements, z_of, element_mesh_fn=boulder_mesh, *, seat_offset_fn=None, sink_mm=1.0, **element_kwargs)` — dispatches to whatever `element_mesh_fn` is passed instead of hardcoding `boulder_mesh`; `seat_offset_fn` defaults to `lambda size: size` (boulders' radius-anchored convention), trees pass `lambda h: 0.0` (base-anchored, since `tree_mesh` already places its trunk base at local z=0). Verify against `tests/test_scatter.py`'s existing call sites that omitting new kwargs reproduces today's exact behavior.
- Add the `TREES` registry entry: `kind='scatter'`, `generator=None`, `placement_fn=scatter_trees`, `element_mesh_fn=tree_mesh`, `uses_feature=False`, `element_kwargs` carrying the internal tree-shape constants (limb count range, taper, segments — deliberately *not* user-tunable, same spirit as boulder roughness), `seat_offset_fn=lambda h: 0.0`, `scatter_prefix="Trees"`, `count_estimate_fn`/`typical_vertex_count_fn` wrapping the new estimators, and exactly 4 `ParamSpec`s to fit the existing `param0..param3` slot budget with no schema changes elsewhere: `min_height_mm`, `max_height_mm`, `density`, and `trunk_radius_mm` (mm-unit, `min=1.0` as a hard print-safety floor — the one knob directly addressing trunk-fragility).

### 2. `hexfinity/scatter.py` (bpy)

- Delete module constants `SINK_MM`, `ROUGHNESS`, `SUBDIV` (now registry-driven per-surface).
- Rewrite `sync_scatter` to look up `surf = ps.SURFACES[region_dict["surface_type"]]` and call `surf.placement_fn(...)` / `ps.assemble_scatter_mesh(..., element_mesh_fn=surf.element_mesh_fn, seat_offset_fn=surf.seat_offset_fn, sink_mm=surf.sink_mm, **{**surf.element_kwargs, **extras})` instead of hardcoding `scatter_boulders`/`boulder_mesh`. Object naming becomes `f"{surf.scatter_prefix}_{name}"`.
- `purge_scatter` and `HEXFINITY_OT_merge_scatter` need **no changes** — already operate generically on any `hf_scatter_of`-tagged child. Broaden their `bl_label`/`bl_description` text from boulder-specific to generic ("Merge Scatter into Tile").

### 3. `hexfinity/panel.py`

- Replace `_draw_scatter_params`'s hardcoded `ps.estimate_boulder_count(...)` + `ps._icosphere(scatter.SUBDIV)` vertex-budget block with registry dispatch through `surf.count_estimate_fn`/`surf.typical_vertex_count_fn`. Drop the now-unused `from . import scatter` import in this module.

### 4. `properties.py` / `operators.py` / `regions.py`

No changes needed — confirmed these layers are already fully registry/surface-agnostic (generic `param0..param3` marshalling, `procedural_surfaces.enum_items()`-driven surface picker).

### 5. Tests — `tests/test_scatter.py`

Mirror the existing `BOULDERS` contract sections (Placement / Mesh / Assembly / Registry) for `TREES`:
- `_capped_cylinder` manifoldness + Euler-characteristic checks across a few radius/segment combos (incl. a cone case).
- `tree_mesh`: manifold, deterministic per pid, differs by pid, limb count within `[min_limbs, max_limbs]` (via the extracted `_tree_limb_count` helper).
- `scatter_trees`: determinism, seed-sensitivity, density-monotonicity, empty-polygon, centres-inside-polygon, heights-within-bounds, bounds-swap-normalized, rotation range.
- Assembly with `element_mesh_fn=tree_mesh, seat_offset_fn=lambda h: 0.0`: manifold, z-offset applied correctly, empty input.
- Registry checks for `TREES` mirroring the `BOULDERS` block (`kind`, `generator is None`, `placement_fn`/`element_mesh_fn` identity, excluded from displace `GENERATING` fan-out, `extra_param_defaults` keys/values/mm-scaling).
- `estimate_tree_count`/`tree_typical_vertex_count` tests mirroring the boulder estimator tests.
- Regression: re-run the full existing `BOULDERS` block unmodified — passing without edits is the acceptance signal that the generalization didn't change boulder behavior.
- Run via Blender's bundled Python: `"C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pytest tests -v`.
- `tests/_headless_region_check.py` currently has no boulder coverage either (bpy-only lifecycle is manual-checklist-only per `docs/boulder-field.md`) — leave as-is; extend the manual checklist in docs instead.

### 6. Documentation

- `CLAUDE.md`: update `procedural_surfaces.py` and `scatter.py` module-map rows to mention the new functions/`Surface` fields and that object naming/sink/roughness are now registry-driven rather than boulder-hardcoded.
- `README.md` (~line 231-236): broaden the "Scatter surfaces" bullet to cover both Boulder Field and Trees; add a link to a new `docs/trees.md`.
- New `docs/trees.md`, mirroring `docs/boulder-field.md`'s structure: algorithm section explaining the overlap-not-weld trunk/limb/canopy construction and why it stays 2-manifold, scene-tree naming, params table, vertex-budget note, tests section, manual checklist (create/seat/determinism/region-edit/merge/export).
- `docs/boulder-field.md`: light cross-reference update noting Trees as the second scatter surface.

## Order of implementation

1. `procedural_surfaces.py`: transform helpers → `_capped_cylinder` → `_roughen` refactor of `boulder_mesh` (run tests, confirm no regression) → `tree_mesh` (+ `_tree_limb_count`) → `scatter_trees` (+ optional `_jittered_grid_placements` extraction, re-run boulder placement tests) → estimators → generalize `Surface`/`assemble_scatter_mesh` (update `BOULDERS` entry, re-run full suite) → add `TREES` entry.
2. `tests/test_scatter.py`: add new Trees/cylinder/registry test blocks; run pytest.
3. `hexfinity/scatter.py`: registry-driven `sync_scatter`; delete old constants; broaden merge-operator text.
4. `hexfinity/panel.py`: generic vertex-budget block; drop `scatter` import.
5. Manual smoke test in real Blender: add a Trees region on a tile, confirm object creation, ground-seating (raycast), per-pid variation, boolean merge, and STL export all work; confirm existing Boulder Field regions on other tiles are unaffected.
6. Docs: `CLAUDE.md`, `README.md`, `docs/trees.md`, `docs/boulder-field.md`.

## Verification

- `"C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pytest tests -v` — full bpy-free suite green, including new Trees tests and unmodified Boulder Field tests (regression signal).
- Manual in-Blender check (per step 5 above) since `scatter.py`/`regions.py`/`panel.py` have no automated coverage.
- Spot-check exported STL of a tile with a Trees region opens cleanly in a slicer (no reported non-manifold warnings), consistent with the boulder field's existing print behavior.

### Critical files
- `hexfinity/procedural_surfaces.py` — all new geometry/placement/registry-generalization
- `hexfinity/scatter.py` — registry-driven `sync_scatter`
- `hexfinity/panel.py` — vertex-budget block
- `hexfinity/manifold_check.py` — read-only reference (confirms edge-multiplicity-only validation)
- `tests/test_scatter.py` — new + regression coverage
