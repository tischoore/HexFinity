"""Pure, bpy-free helpers for the per-tile STL export feature.

Kept free of any `bpy` import so the pytest suite can exercise the dedup and
naming logic against Blender's bundled CPython without launching Blender (see
the bpy-free rule in CLAUDE.md). The operator in `operators.py` feeds these
functions evaluated mesh data and consumes the filenames / hashes they return.

Dedup contract
--------------
Two tiles export to the *same* file iff their final, built geometry is
identical. We hash the **result** (tile-local vertices + faces, plus each child
terrain object's mesh in tile-local space) rather than enumerating the dozens of
parameters that feed the builder. That automatically folds in corner heights,
dome shape, brush/snap displacement, and procedural surface regions (whose seed
depends on (q, r), so region tiles never falsely dedup across coordinates).
"""

import hashlib

# Quantization step for vertex coordinates before hashing, in millimetres.
# Float jitter below this threshold (e.g. from matrix round-trips) collapses to
# the same bucket so visually identical tiles dedup reliably. 1e-4 mm = 0.1 µm,
# far finer than any printer resolves yet coarse enough to absorb FP noise.
_QUANT_MM = 1.0e-4


def _quantize(value):
    """Snap a float to the export quantization grid, returning an int bucket.

    Using an int (rounded multiple of ``_QUANT_MM``) keeps the hashed
    representation exact and platform-independent — no float repr drift.
    """
    return int(round(value / _QUANT_MM))


def _mesh_digest(verts, faces):
    """SHA-256 over a single mesh's quantized verts + face vertex indices.

    `verts` is an iterable of (x, y, z); `faces` an iterable of index tuples.
    Vertex and face order are taken as-is — the hex builder is deterministic, so
    the same inputs always yield the same ordering.
    """
    h = hashlib.sha256()
    for (x, y, z) in verts:
        h.update(b"v")
        h.update(str(_quantize(x)).encode())
        h.update(b",")
        h.update(str(_quantize(y)).encode())
        h.update(b",")
        h.update(str(_quantize(z)).encode())
    for face in faces:
        h.update(b"f")
        h.update(",".join(str(int(i)) for i in face).encode())
    return h.hexdigest()


def tile_geometry_hash(verts, faces, children=None):
    """Deterministic content hash of a tile's full exported geometry.

    Parameters
    ----------
    verts, faces : the hex tile's own mesh, in tile-local coordinates.
    children : optional iterable of ``(verts, faces)`` for parented terrain
        objects, already transformed into tile-local space.

    Child digests are **sorted** before folding in, so the order in which
    terrain objects happen to be parented/iterated does not affect the result —
    two tiles carrying the same set of objects at the same relative poses hash
    equal regardless of object order.
    """
    base = _mesh_digest(verts, faces)
    child_digests = sorted(_mesh_digest(cv, cf) for (cv, cf) in (children or []))

    h = hashlib.sha256()
    h.update(base.encode())
    for d in child_digests:
        h.update(b"|")
        h.update(d.encode())
    return h.hexdigest()


def short_hash(digest, n=8):
    """First `n` hex characters of a digest — the human-facing filename suffix."""
    return digest[:n]


def is_custom_tile(has_children, has_brush, has_snap, region_count):
    """Whether a tile counts as user-customized for naming purposes.

    Custom tiles get a content-hash suffix appended to their coordinate name;
    plain tiles are named by coordinate alone. A tile is custom if it carries
    any terrain objects, brush displacement, snap displacement, or one or more
    procedural surface regions.
    """
    return bool(has_children or has_brush or has_snap or region_count > 0)


def tile_filename(q, r, custom, short):
    """STL filename for a tile.

    Plain  -> ``hex_q00_r00.stl``
    Custom -> ``hex_q00_r00_<short>.stl`` (``short`` from :func:`short_hash`).

    The coordinate prefix tells you placement; the hash suffix guarantees two
    differing custom tiles never collide and lets byte-identical custom tiles
    share one file.
    """
    stem = f"hex_q{q:02d}_r{r:02d}"
    if custom:
        stem = f"{stem}_{short}"
    return f"{stem}.stl"


def flora_placement_filename(q, r, index):
    """STL filename for one planted tree, tree and pin merged into one file.

    Shares the `hex_qNN_rNN` stem used by `tile_filename` (instead of the old
    `flora_` prefix) so a tile's own STL and its trees' STLs sort together in
    a file browser. No content-hash suffix: unlike tiles/the old fused
    flora export, each placement now always gets its own file rather than
    sharing one across byte-identical placements, so the hash added nothing.
    """
    return f"hex_q{q:02d}_r{r:02d}_tree{index:02d}.stl"


def flora_manifest_rows(records):
    """Normalize flora export records into serializable manifest rows.

    `records` is an iterable of dicts with ``q``, ``r``, ``index`` and
    ``file`` keys — one row per placement. Kept as its own schema rather than
    overloading `manifest_rows`, since every flora row means something
    slightly different from a tile row. Sorted by (q, r, index) for a
    stable, diffable manifest.
    """
    rows = [
        {
            "q": int(rec["q"]),
            "r": int(rec["r"]),
            "index": int(rec["index"]),
            "file": str(rec["file"]),
        }
        for rec in records
    ]
    rows.sort(key=lambda row: (row["q"], row["r"], row["index"]))
    return rows


def manifest_rows(records):
    """Normalize export records into serializable manifest rows.

    `records` is an iterable of dicts with at least ``q``, ``r``, ``file`` and
    ``custom`` keys. Rows are sorted by (q, r) so the manifest is stable across
    runs (handy for diffing / version control of a print job).
    """
    rows = [
        {
            "q": int(rec["q"]),
            "r": int(rec["r"]),
            "file": str(rec["file"]),
            "custom": bool(rec["custom"]),
        }
        for rec in records
    ]
    rows.sort(key=lambda row: (row["q"], row["r"]))
    return rows
