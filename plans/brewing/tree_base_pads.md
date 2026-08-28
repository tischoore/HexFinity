# Investigation: tessellating a flat pad under planted trees

**Question asked:** Can we tessellate the terrain around a tree's base so the tree sits flush and fully horizontal, morphing organically back into the hex? Should `penetration_mm` be removed if we do?

**Verdict: yes, feasible, and it fits the existing architecture unusually well.** Keep `penetration_mm`, but change its default from 2.0 mm to 0.3 mm — its job changes rather than disappearing. Evidence below.

---

## Part 1 — Findings

### F1. The problem is real and is purely a terrain problem

Trees are already forced world-vertical: `flora.py:210` sets `rotation_euler = (0.0, 0.0, p.rotation_rad)` — X and Y euler are hard zero, and the surface normal returned by the seating raycast is explicitly discarded as `_n` at `flora.py:189`. The tree STL has a **flat base cut**. So on sloped ground the flat base meets a sloped surface along one edge: the uphill side pokes out, the downhill side floats.

This confirms the framing in the request — normal-aligning the tree is *not* the fix (it would tilt the tree). The terrain has to adapt.

### F2. Flattening only the existing vertices would not work — tessellation is genuinely required

Top-surface vertex spacing is `side_len / 2**(smoothness_passes + resample_density)`. On a 100 mm tile at the default `smoothness_passes=2, resample_density=0`, that is **12.5 mm**, with 157 top verts total (`top_vertex_count`, `mesh_builder.py:100-115`).

A tree base at the default `man_height_mm=10` is roughly **1–3 mm** across. So a small tree frequently has **zero vertices underneath it**. Lerping the existing verts toward a pad height would either do nothing or produce a ~12 mm crater around a 2 mm trunk.

The blunt alternative — raising per-tile `local_subdiv` (`properties.py:525-537`) — is uniform, so each pass quadruples the *whole tile*. Reaching ~1 mm spacing needs `s+r≈6` → **36 937 verts and 73 728 top tris per tile**, which is exactly what the existing panel vertex-budget warning exists to prevent. It also invalidates the brush and snap layers on every change (`operators.py:139-144`).

Local, adaptive refinement is the only approach that works at default settings. Cost is roughly **+30 verts per tree** instead of 4ⁿ for the tile.

### F3. Appending vertices after the `num_top` prefix is safe — this is the key enabler

The `hf_brush_disp` / `hf_snap_disp` layer contract keys on indices `0 .. num_top-1`. I checked every consumer, and they all guard with `<`, never `==`:

- `brush.py:192` — `if len(mesh.vertices) < ntop`
- `operators.py:1011` — `if len(tile.data.vertices) < num_top`
- `mesh_builder.py:385` — `len(top_displacement) == num_top`, compared against the closed-form count, not the actual vertex list

So new vertices inserted *after* the top prefix and *before* the bottom/wall verts leave `top_vertex_count()`, the brush layer, and the snap layer completely untouched. **If instead we redefined `num_top` to include the new verts, every layer would be invalidated on every plant/unplant** — that is the critical design lever, and it costs nothing to get right.

### F4. All bottom, wall and tab geometry is decoupled from interior top topology

The side wall reads top verts only through `top_rim_key` (`mesh_builder.py:415-420`), consumed by a single n-gon per side (`mesh_builder.py:544-559`). Refining strictly *interior* triangles leaves walls, tabs, holes and the base plate byte-identical. The constraint that follows: **rim edges must never be split**, or `rim_density` and the wall n-gon break.

### F5. Refinement must be crack-free, and the codebase will catch us if it isn't

`assert_two_manifold` (`manifold_check.py:30-35`) fails any undirected edge not used by exactly 2 faces — which is precisely a T-junction detector. A one-sided edge split produces three failing edges and a loud build failure. Good: correctness here is enforced, not hoped for.

Note also that refinement must run **after** `subdivide_loop`, never inside the control mesh, because the interior-edge stencil asserts exactly 2 incident faces (`subdivision.py:101-103`).

A per-*edge* split decision (rather than per-triangle) is crack-free by construction, needs no closure iteration, and sidesteps all of this.

### F6. The "flatten a footprint, blend organically outward" algorithm already exists in the repo

`operators._compute_snap_gap` (`operators.py:892-979`) is the snap-to-model feature, and it is structurally the same problem:

- core weight `1.0` inside the footprint (`operators.py:947`)
- `_smooth_falloff` skirt on distance to the nearest core vert (`operators.py:973`)
- **a rim fade so a model near a hex edge cannot desync the seam** (`operators.py:975-976`)

The tree case is strictly simpler: the footprint is a *disc* of known radius, not a raycast result. That means it needs no `bpy` at all and can live in the tested, bpy-free layer. The rim fade is the non-obvious part to copy — without it, a tree near an edge would move the seam and break interlock with the neighbour tile.

### F7. Pad height should be a lerp, not another additive layer

Current composition is additive (`mesh_builder.py:389-398`):
`z = max(z_subdiv + brush + snap + rim_fade·Σ(mask·surface), base_thickness)`.

If the pad were an additive term it would be *fought by the procedural surfaces*, which are added afterwards — cobblestone and gravel bumps would still appear on the supposedly flat pad. Making the pad a **lerp toward the pad height** erases procedural bumps *and* brush strokes inside the pad automatically, and needs **no change** to the `fade` term at `mesh_builder.py:393-397`. (The alternative — multiplying that fade by a pad mask — handles procedural surfaces but not brush paint.)

### F8. Pad radius can be derived automatically, no slider needed

`flora._get_or_import_mesh` already walks every vertex once and caches `min_z` plus the XY bbox (`flora.py:121-134`). The bbox is the **canopy**, far too wide for a pad. But the same walk can cheaply yield the true **base-cut radius**: among verts within epsilon of `min_z`, the max XY distance from their centroid. That is literally the flat face that must sit flush — self-tuning per species, and it survives asset changes and rescaling.

### F9. Re-seating is free

`sync_flora`'s downward raycast (`flora.py:188-194`) samples the tile's current surface, and `rebuild_tile` already purges and re-syncs flora *after* the new mesh is assigned and the depsgraph is updated (`operators.py:232-243`). So once the pad is in the mesh, the raycast lands on it and the tree is flush and level with **no change to the seating math**. The ordering is already correct.

### F10. Advice on `penetration_mm` — keep it, drop the default

**Do not remove it.** Its original job (hide the slope gap) is fully superseded by pads, but it retains a second job pads cannot do: a tree base exactly coplanar with a flat pad **z-fights in the viewport and is a degenerate zero-thickness contact for slicers and booleans**. It is also the only fallback when `flatten_base` is off, or when the rim fade shrinks a pad near a seam (see L1).

So: keep the property, change the default `2.0 → 0.3` mm — reframing it from "hide the gap" to "guarantee a real bite". Keep it **absolute mm rather than scaling it by `man_height_mm`**: under the new role it is a print/render tolerance, which is correctly scale-independent. (It looks like a bug that it doesn't scale; under the new role it is right.)

One genuine bug to fix while there: `penetration_mm` has **no `update=` callback**, so dragging the slider does nothing until something else happens to trigger a rebuild — contradicting the checklist step in `docs/flora.md` that says the trees should visibly sink.

### F11. Alternative considered and rejected: boolean socket + peg

`tree_plant_01.md` §8 sketches cutting cylindrical sockets into the tile and giving trees pegs. It would guarantee flush and printable, but it is a bpy-side boolean **re-cut on every rebuild** (its own caveat at ~line 423), it lives outside the bpy-free tested layer, and it requires changing the tree assets. The pad approach achieves the same visual result inside the existing tested architecture, and does not preclude sockets later.

---

## Part 2 — Implementation plan

### New module: `hexfinity/tree_pads.py` (bpy-free)

Must not import `bpy` (CLAUDE.md invariant), joining the pytest-able set alongside `mesh_builder.py`, `subdivision.py`, `procedural_surfaces.py`.

```python
def sample_surface_z(verts, faces, x, y) -> float
def refine_and_flatten(verts, faces, protected_edges, pads,
                       diameter_mm, base_thickness_mm) -> faces
```

**`sample_surface_z`** — locate the triangle containing `(x, y)`, barycentrically interpolate z, fall back to nearest-vertex z. Used to pick each pad's target height from the *pre-flatten* surface. All pad heights are sampled up front so two nearby pads cannot influence each other's target.

**`refine_and_flatten`** — appends new verts to `verts` in place (per F3) and returns retriangulated top faces.

*Refinement*, up to `MAX_LEVELS = 4`, stopping early when no edge qualifies:
- Build the edge→faces map over the current top triangles.
- Mark an edge when it is **not** in `protected_edges` (F4), its segment comes within `r_pad + r_blend` of a pad centre, and its length exceeds `target_len = r_pad * 0.6`.
- Insert one midpoint per marked edge, deduped by a `(min_idx, max_idx)` key, at the chord midpoint — same semantics as `linear_midpoint_subdivide` (`subdivision.py:184-217`), so surface shape is preserved.
- Retriangulate each face by its marked-edge count with templates preserving the parent's +Z winding: 0→keep, 1→2 tris, 2→3 tris (quad split on the shorter diagonal), 3→4 tris.

Crack-free by construction (F5): the split decision and midpoint index are per-edge and globally shared, so two faces across an edge always agree.

*Flattening* — for every top vert, originals **and** new, per pad:
```
d  = hypot(x - x0, y - y0)
w  = 1.0 if d <= r_pad else smoothstep(1 - (d - r_pad) / r_blend)   # 0 beyond
w *= clamp(rim_edge_distance(x, y, diameter_mm) / r_blend, 0, 1)    # seam guard, per F6
z  = z + w * (pad_z - z)                                            # lerp, per F7
```
then `max(z, base_thickness_mm)`. Reuses `mesh_builder.rim_edge_distance` (`mesh_builder.py:77-97`).

### `hexfinity/mesh_builder.py`

Add a `flora_pads=None` kwarg. Integration is a small window between the displacement loop (ends `:398`) and top-face emission (`:400-401`):
- In the existing rim-registration loop (`:362-372`), also collect consecutive rim vertex-index pairs into a `protected_edges` set.
- Remap `sub_faces` to new indices, then when `flora_pads` is non-empty pass them through `tree_pads.refine_and_flatten` before `faces.extend(...)`.

`num_top`, `top_vertex_count` and both displacement layers are unchanged — new verts land at indices `>= num_top`, ahead of the bottom/wall verts `add_vert` registers afterwards.

### `hexfinity/flora.py`

- Extend the vertex walk in `_get_or_import_mesh` (`:121-134`) to also track `max_z` and compute **`base_radius`** per F8, with `eps = max(1e-4, (max_z - min_z) * 0.002)` and degenerate fallback `max(half_x, half_y) * 0.15`. Cache in a new `_mesh_base_radius` dict, evicted alongside the others at `:96-98`.
- Append `base_radius` to the return tuple; update the three unpack sites (`sync_flora`, `_place_tree`, `_placement_footprint`).
- New `pad_specs(tile_obj) -> list[dict]`: returns `[]` immediately when there are no placements or `flatten_base` is off (so treeless tiles pay nothing and trigger no STL import), else one `{"x", "y", "radius_mm", "blend_mm"}` per placement, with `radius_mm = base_radius * scale_factor * global_scale * PAD_MARGIN` (module constant `PAD_MARGIN = 1.25`).
- `sync_flora` seating math unchanged (F9).

### `hexfinity/operators.py`

- In `rebuild_tile`, compute `flora_pads = flora.pad_specs(obj)` before `build_hex_tile` (`:189-204`) and pass it through.
- In `_compute_snap_gap`, pass `flora_pads=None` explicitly (`:908-920`) — that build is the clean undisplaced baseline and must not bake pads into the snap gap.
- Add `_rebuild_flora_tiles(context)` for the new update callbacks (iterate map tiles with placements, call `rebuild_tile`).

### `hexfinity/properties.py` / `hexfinity/panel.py`

On `HexFinityFloraProperties` (`properties.py:128-168`):
- `flatten_base: BoolProperty` (default `True`, `update=` rebuild)
- `pad_blend_mm: FloatProperty` (default `3.0`, min 0, soft_max 20, `update=` rebuild)
- `penetration_mm`: default `2.0 → 0.3`, description rewritten, `update=` callback added (F10)

In the Flora box (`panel.py:153-169`), insert `flatten_base` and `pad_blend_mm` above `penetration_mm`, greying the blend row via `row.enabled` when the toggle is off (same idiom as the existing `min_spacing_mm` row). Update the vertex-budget note at `panel.py:237` to mention pads add a bounded number of verts beyond `top_vertex_count`.

---

## Part 3 — Known limitations (document, don't fix in v1)

- **L1. Pad crossing a hex seam.** The rim fade shrinks a pad within `pad_blend_mm` of an edge, so such a tree is less flush on its outward side. This is the deliberate trade for seam integrity; propagating pads to the neighbouring tile is a follow-up.
- **L2. Brush resolution inside a pad.** Refined verts are recreated each rebuild by interpolating painted parents, so paint detail inside a pad is capped at base resolution. Intentional — it is what keeps `hf_brush_disp` topology-stable.
- **L3. Overlapping pads** apply sequentially (last wins). Rare, since `avoid_overlap` already rejects colliding placements.

## Part 4 — Files

| File | Change |
|---|---|
| `hexfinity/tree_pads.py` | **new**, bpy-free — refinement + flattening + `sample_surface_z` |
| `hexfinity/mesh_builder.py` | `flora_pads` kwarg; `protected_edges` set; call site between `:398` and `:400` |
| `hexfinity/flora.py` | `base_radius` extraction + cache; `pad_specs()`; `PAD_MARGIN` |
| `hexfinity/operators.py` | pass `flora_pads` in `rebuild_tile`; `None` in `_compute_snap_gap`; rebuild helper |
| `hexfinity/properties.py` | `flatten_base`, `pad_blend_mm`; `penetration_mm` default + update |
| `hexfinity/panel.py` | Flora box rows; vertex-budget note |
| `tests/test_tree_pads.py` | **new** |
| `tests/test_mesh_builder.py` | extend `test_top_vertex_count_matches_builder` for the `flora_pads=None` path |
| `tests/_headless_flora_pad_check.py` | **new** manual checklist, mirroring `_headless_region_check.py` |
| `README.md`, `docs/flora.md` | geometry + UI docs |

## Part 5 — Verification

**Automated** — `"C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pytest tests -v`. New `tests/test_tree_pads.py`:

1. `assert_two_manifold(verts, faces)` on a padded tile — the crack-free/T-junction proof (F5).
2. **Rim untouched** — rim vert positions and side-wall n-gon vertex count identical with and without pads (F4).
3. **Layer prefix stable** — `verts[0:num_top]` XY identical with/without pads; `top_vertex_count(s, r)` still equals the no-pad vertex count (F3).
4. **Pad is flat** — on a tile with corners at different levels, every vert within `r_pad` has `|z - pad_z| < 1e-6`.
5. **Blends cleanly** — verts beyond `r_pad + r_blend` are bit-identical to the no-pad build.
6. **Local + bounded** — a treeless tile gains zero verts; a one-tree tile gains a bounded count, all near the pad (F2).
7. **Determinism** — two identical builds produce identical output.
8. **Rim fade** — a pad centred near an edge leaves rim verts untouched (F6/L1).
9. **Beats procedural texture** — with a cobblestone region active the pad interior is still flat, proving the lerp rather than the additive path (F7).
10. `sample_surface_z` matches known planar geometry.

**Manual (in Blender via `deploy.ps1`)** — the `bpy`-touching parts have no automated coverage:
1. Generate a map, raise one corner for a steep slope, plant a tree → pad appears, tree sits flush and level, terrain blends smoothly outward.
2. Plant on flat ground → visually unchanged from today.
3. Toggle `flatten_base` off → old sunken look returns; on → pad returns.
4. Drag `pad_blend_mm` and `penetration_mm` → both re-seat live (proves the new update callbacks, F10).
5. Drag a corner slider with a tree planted → pad follows the new surface, tree stays flush (F9).
6. Paint with the terrain brush across a pad → pad tracks the paint and stays flat.
7. Plant near a hex edge → seam with the neighbour stays aligned, no gap or step (L1).
8. Export tiles → padded tile is a valid manifold STL.
