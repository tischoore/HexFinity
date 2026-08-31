# Per-tile STL export

The **Export Tiles to STL** button (bottom of the HexFinity N-panel, shown once a
map exists) writes one `.stl` per *distinct* hex tile plus a coordinate→file
manifest. This page documents the dedup contract and the manifest format.

## Workflow

1. Click **Export Tiles to STL**. A directory browser opens.
2. Navigate to the parent location and (optionally) edit the **Subfolder** name in
   the operator sidebar (default `hexfinity_export`). The export writes into
   `<chosen dir>/<subfolder>/`, creating it if needed.
3. The operator iterates every tile in the map collection, hashes its geometry,
   exports each distinct tile once, and writes the manifest.

Each exported tile is temporarily moved so its origin sits at the world origin
before writing, so every STL is centered and drops straight onto a printer bed. The
tile's real position is restored afterwards (even if an export fails).

## What gets exported per tile

STL has no concept of separate objects — it is raw triangles. The exporter selects
the hex tile **plus every mesh object parented to it** (the terrain objects placed by
*Terrain Objects → import*) and writes them together, so they merge into one triangle
soup in the file. No boolean union is performed; overlapping shells are left for the
slicer to resolve (every modern slicer handles this).

## Dedup contract

Two tiles export to the **same file** if and only if their final, built geometry is
identical. The dedup key is a SHA-256 content hash computed in the bpy-free
`tile_export.py` module:

- The hex's evaluated mesh, in **tile-local** coordinates.
- Each child terrain object's evaluated mesh, transformed into the **tile's local
  frame** (`tile.matrix_world.inverted() @ child.matrix_world`).
- Vertex coordinates are quantized to a 1e-4 mm grid before hashing, so
  floating-point jitter from matrix round-trips does not split otherwise-identical
  tiles into separate files.
- Child mesh digests are **sorted** before folding in, so the order terrain objects
  happen to be parented in does not affect the hash.

Hashing the *result* rather than the dozens of parameters that feed the builder means
corner heights, dome shape, brush displacement (`hf_brush_disp`), terrain-object
plateau pads, and procedural surface regions are **all** captured automatically.
Surface-region patterns are seeded from `(q, r)`, so two region tiles at different
coordinates produce different geometry and correctly do **not** dedup.

## Naming

| Tile kind | Filename | Example |
|---|---|---|
| Plain (no customization) | `hex_q{q:02d}_r{r:02d}.stl` | `hex_q00_r00.stl` |
| Custom | `hex_q{q:02d}_r{r:02d}_{hash8}.stl` | `hex_q03_r05_1a2b3c4d.stl` |

A tile counts as **custom** if it has any of: parented terrain objects, terrain-brush
displacement, a terrain-object plateau, or one or more procedural surface regions
(`tile_export.is_custom_tile`). The 8-char suffix is the first 8 hex characters of the
geometry hash, so differing custom tiles never collide and byte-identical custom tiles
share one file.

When duplicates collapse, the surviving file is named from the **first** tile of the
group encountered during iteration; the manifest records every coordinate that maps to
it.

## Manifest

Two files are written next to the STLs:

- `manifest.csv` — columns `q, r, file, custom`.
- `manifest.json` — the same rows as a JSON array.

Rows are sorted by `(q, r)` for stable diffs. Every coordinate in the map appears —
including ones whose geometry was deduped to a shared file — so you can place each
printed tile correctly.

Example `manifest.csv`:

```
q,r,file,custom
0,0,hex_q00_r00.stl,False
0,1,hex_q00_r00.stl,False
1,0,hex_q01_r00_1a2b3c4d.stl,True
```

Here `(0,0)` and `(0,1)` are identical plain tiles sharing one STL, and `(1,0)` is a
customized tile with its own hash-suffixed file.

## Code map

- `hexfinity/tile_export.py` — bpy-free: `tile_geometry_hash`, `short_hash`,
  `is_custom_tile`, `tile_filename`, `manifest_rows`. Unit-tested by
  `tests/test_tile_export.py`.
- `hexfinity/operators.py` — `HEXFINITY_OT_export_tiles`: the directory dialog,
  depsgraph mesh evaluation, dedup loop, `bpy.ops.wm.stl_export` calls, and manifest
  writing.
- `hexfinity/panel.py` — the **Export** box at the bottom of the panel.
