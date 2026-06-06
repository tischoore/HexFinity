# Boulder Field — the scatter surface kind

**Boulder Field** is the first member of a second *kind* of procedural surface:
`scatter`. Where a displacement surface (cobblestone, gravel, furrow) bakes a
heightfield into the tile top, a scatter surface places **distinct objects** in
the region and **leaves the tile surface untouched**.

This page covers the boulder algorithm, the scene tree it builds, merging for
print, and the manual checklist for the bpy-only parts. The shared region
authoring UX and the displacement kind are in
[procedural_surfaces.md](procedural_surfaces.md); the geometry baseline is in the
main [README](../README.md).

## Two kinds of parametric surface

| Kind | Output | Examples | Pipeline |
|---|---|---|---|
| `displace` | per-point Z offset; **changes the surface** | Cobblestone, Gravel, Furrow | `generator(x,y,…) → z` → `mesh_builder` |
| `scatter` | **distinct objects**; surface untouched | Boulder Field | `placement_fn` + `element_mesh_fn` → `scatter.py` joins one mesh |

The registry is the single source of truth. A `Surface` record carries a `kind`
(default `'displace'`). A scatter surface sets `kind='scatter'`,
`generator=None`, and instead carries `placement_fn` + `element_mesh_fn` plus a
tuple of `ParamSpec` extra parameters. Because `generator is None`:

- `surface_offset()` returns `0.0` for it → it contributes **zero displacement**
  automatically, and `mesh_builder` skips it with no change;
- the displacement test fan-out (`[k for k,s in SURFACES.items() if s.generator]`)
  **excludes** it automatically → none of the heightfield contract tests apply.

## The pure / impure split (the bpy-free invariant)

All geometry math is bpy-free, unit-tested, and manifold-checked, exactly like
`mesh_builder`. Only object instantiation, the seating raycast, and the boolean
merge touch `bpy`.

### bpy-free — `procedural_surfaces.py`

1. **Placement** — `scatter_boulders(polygon, *, min_size_mm, max_size_mm,
   density, distribution, seed) → [(x, y, radius, rot, pid)]` in tile-local XY (Z
   resolved later). A jittered grid drives it: the cell **pitch** and a per-cell
   **presence probability** both tighten with `density`, so more density → more
   boulders. Each kept cell draws a radius from `[min,max]/2` shaped by
   `distribution` (0 = many small + few large, 1 = uniform) and a random rotation.
   Only cells whose centre is inside the polygon are kept. Deterministic from
   `seed` → stable rebuilds.
2. **Boulder mesh** — `boulder_mesh(radius_mm, pid, *, roughness, subdiv) →
   (verts, faces)`: a programmatic icosphere (`_icosphere` — 12 base verts,
   edge-subdivide with a shared midpoint cache, normalise) with per-vertex
   **radial** noise from `pid`. Because every vertex only moves along its own
   radius, the topology — and 2-manifoldness — is preserved.
3. **Assembly** — `assemble_scatter_mesh(placements, z_of, *, sink_mm, …) →
   (verts, faces)`: builds each boulder, rotates about Z, translates to XY, seats
   it so its centre sits at `z_of(x, y) + radius − sink_mm`, and concatenates into
   ONE mesh. `z_of` is injected by the caller, so this stays bpy-free; the boulders
   share no vertices, so the union of 2-manifold boulders is itself 2-manifold.

`estimate_boulder_count(polygon, …)` and `polygon_area(polygon)` back the panel's
vertex-budget warning.

### bpy shell — `scatter.py`

`sync_scatter(tile_obj, region_idx, region_dict, name)`:

1. `scatter_boulders(...)` → placements (whole-tile regions use
   `hex_polygon(diameter)`).
2. `z_of(x, y)` = raycast straight **down** onto `tile_obj` (a downward
   `obj.ray_cast` in tile-local space) → seats each boulder on the *real* terrain,
   after Coons / displacement / brush / height edits — robust regardless of how
   the height was produced. A small `SINK_MM` overlap keeps a later boolean clean.
3. `assemble_scatter_mesh(...)` → one mesh; create object `Boulders_<Area Name>`,
   **parent to `tile_obj`** (identity parent inverse, so the boulder verts — already
   tile-local — land in the right place), link into the tile's collection(s), and
   tag `obj["hf_scatter_of"] = "region_<idx>"`.

The tile rebuild **purges all `hf_scatter_*` children first**, then recreates from
the current scatter regions — robust to region add/remove/reorder. This runs
inside `operators._REBUILDING` (so the object churn never recurses) and *after*
the new tile mesh is assigned and the depsgraph re-evaluated (so the raycast
samples the current surface, never stale geometry).

`HEXFINITY_OT_merge_scatter` (Merge button): boolean-unions the boulder object(s)
into the tile mesh with an EXACT solver, removes the loose object(s), and
re-checks 2-manifoldness. Regenerating the tile recreates the loose boulders, so
the loose object stays the editable source.

## Scene tree

```
HexFinity Map (collection)
└─ HexTile_00_00            (the tile object)
   └─ Boulders_Area 1       (joined boulder mesh, parented under the tile)
```

Boulder objects are ordinary mesh children, so the **STL export picks them up
automatically** as part of the tile (loose, unless you Merge first). Names dedupe
with a `_00`/`_01` suffix when two areas share a name.

## Parameters

| Param | Meaning |
|---|---|
| **Area Name** | Editable label; names the object `Boulders_<name>`. Auto-defaults to `Area N`. |
| **Min / Max Boulder Size (mm)** | Diameter range. mm params, so their defaults scale with **Man Height**. |
| **Boulder Density** | 0 = sparse scatter, 1 = packed field (drives both grid pitch and per-cell presence). |
| **Size Distribution** | 0 = many small + few large, 1 = uniform sizes. |
| **Merge into Tile** | Per-region preference for the Merge button (printability). |

> **Vertex budget.** Density × area × small sizes can yield a very heavy joined
> mesh. The panel estimates the boulder/vertex count live and warns past ~200k
> verts — raise the sizes or lower the density, or draw a smaller region.

## Tests (bpy-free)

`tests/test_scatter.py` covers: placement determinism, density monotonicity,
centres-inside-polygon, radii within bounds, distribution histogram shift; boulder
mesh manifoldness, vertex-radius bounds, per-`pid` determinism; assembly
manifoldness, summed counts, injected-`z_of` offsets; and the registry contract
(scatter excluded from the displacement fan-out, zero displacement, extra-param
defaults and mm scaling). Run:

```
"C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pytest tests -v
```

## Manual checklist (the bpy-only parts)

The object lifecycle, raycast seating, and boolean merge can't be unit-tested
without Blender. Verify by hand after changes:

1. **Create** — generate a single tile, select it, add a region, set surface =
   *Boulder Field*. A `Boulders_<Area Name>` object appears parented under the
   tile, populated with boulders.
2. **Seating** — boulders rest on the surface (not floating, not fully buried).
   Raise/lower a corner or paint the brush: on the rebuild the boulders re-seat on
   the new height.
3. **Determinism** — a pure height edit regenerates the same number of boulders in
   the same places.
4. **Region edits** — adding / removing / reordering regions never leaves orphan
   boulder objects; removing the region removes its boulders.
5. **Merge** — click *Merge Boulders into Tile*: the loose object disappears, the
   tile vertex count jumps, and the result stays manifold (no warning).
6. **Export** — *Export Tiles to STL* includes the boulders (loose if not merged,
   unioned if merged); a tile with a scatter region is treated as custom.
