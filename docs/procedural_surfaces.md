# Procedural surface textures

Geometric micro-surfaces — cobblestone, gravel, plough & furrow — baked onto the
top of a hex tile as real geometry (not a render-time material), so they print.
They are applied through **regions**: closed loops you draw on the tile, each with
its own surface type, scale, and direction. Multiple regions per tile are
supported (a cobblestone road *and* a furrowed field on the same hex).

This page covers the workflow and the model. The geometry baseline (Coons-free
Loop-subdivided top, manifold guarantee, interlocks) is in the main
[README](../README.md).

## How it works (the short version)

A surface is a **pure function of a vertex's XY position** — `z += f(x, y)`. On
every rebuild, `build_hex_tile` walks the top vertices and adds, per vertex:

```
rim_fade(x, y) · Σ_regions  region_mask(x, y) · surface_offset(x, y, …)
```

then clamps to the base thickness. Consequences:

- **It is data, not a baked mesh.** Like the corner levels and the terrain brush,
  it is recomputed from parameters on every rebuild, so it survives height edits.
- **Regions live in continuous XY**, independent of vertex count — so changing
  subdivision/resample re-evaluates the texture cleanly (unlike the painted brush
  layer, which is keyed to a fixed vertex count and is dropped on a resolution
  change).
- **Seams stay flat.** `rim_fade` damps every surface to zero within a band of the
  hex rim, so shared edges/corners keep their exact interlock heights.
- **Patterns flow across tiles.** Sampling uses global coordinates (tile world XY
  as the origin), so a road continues across the seam onto the neighbour.

## Scale: man height drives the defaults

Everything is in millimetres. A surface needs a *feature size* (cobble width,
pebble size, furrow pitch) at the model's scale. Rather than guess, set **Man
Height (mm)** in *Map Globals* — the printed height of a human figure (28 mm is a
common wargaming scale). When you pick a surface type, its feature size is
auto-filled from a real-world reference scaled to your model:

```
feature_mm = reference_mm · man_height_mm / 1800     (1800 mm ≈ a real human)
```

| Surface      | Real-world reference | At man = 28 mm |
|--------------|----------------------|----------------|
| Cobblestone  | 120 mm               | ~1.9 mm        |
| Gravel       | 30 mm                | ~0.5 mm        |
| Plough/furrow| 700 mm (pitch)       | ~10.9 mm       |

The auto-filled value is just a starting point — every region's **Feature Size**,
**Depth**, **Regularity**, **Direction**, and **Edge Blend** stay editable.

### Varied-but-recognizable (the cobblestone problem)

Cobblestone and gravel are built on **jittered Voronoi (Worley) cells**: a grid of
cell centres nudged by a per-cell random offset. **Regularity** (0–1) is that
jitter: `0` gives neat regular courses, `1` gives irregular cobbles — the knob that
makes stones look natural without being noise.

## The resolution ceiling (read this before going fine)

The surface is a **heightfield** — it only moves existing top vertices in Z; it
adds no new topology. So detail is bounded by the top-vertex spacing, which is set
by **Smoothness Passes + Resample Density + Local Subdivision**:

| Passes (≈ on a 220 mm tile) | Top verts | Vertex spacing |
|-----------------------------|-----------|----------------|
| 2                           | 157       | ~16 mm         |
| 3                           | 601       | ~8 mm          |
| 4                           | 2353      | ~4 mm          |

A feature finer than ~2× the vertex spacing cannot be resolved — it will alias or
flatten out. The panel shows the current spacing and warns when your Feature Size
is too fine for it; raise **Local Subdivision** on that tile (or map-wide
Resample/Smoothness) until the warning clears. This is the right model for 3D
printing — you want baked geometry, not render-time microdisplacement — but it
means fine detail on a large map is expensive, so subdivide per-tile where needed.

## Workflow

1. Select a generated tile.
2. *HexFinity → Procedural Surface → **Draw Region***.
3. Click points on the tile surface to outline a loop. **Backspace** removes the
   last point; **Enter** or **RMB** closes the loop; **Esc** cancels.
4. The new region is created (cobblestone by default) and selected. Tweak its
   **Surface**, **Feature Size**, **Depth**, **Regularity**, **Direction**, and
   **Edge Blend** in the panel.
5. For anisotropic surfaces (furrows), set **Direction (deg)** — a green arrow on
   the active region shows which way the furrows run.
6. Add more regions with **Draw Region** again, or the **＋** button for a
   whole-tile region (no outline). **－** removes the active region.

## Extending: adding a new surface type

The surface set is a registry in `hexfinity/procedural_surfaces.py` (the single
source of truth — the enum, the scale defaults, and the per-point dispatch all
derive from it). Adding one is a single localized change:

1. Write a generator
   `def _myfx(x, y, *, feature_mm, depth_mm, regularity, seed, direction_rad=0.0) -> float`
   returning a Z offset bounded by `depth_mm`.
2. Add one `Surface(...)` record to `SURFACES`.

No edits to `mesh_builder`, `operators`, `properties`, `panel`, or the tests are
needed — they all read the registry, and the registry-parametrised test suite in
`tests/test_procedural_surfaces.py` covers the new surface automatically. Planned
additions: **Tiles** (regular Voronoi, sharp edges) and **Cracked** (irregular
Voronoi edges carved as valleys).
