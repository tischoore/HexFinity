"""Procedural geometric surface textures for hex tops.

No `bpy` imports — this module is unit-testable in plain CPython (same bpy-free
rule as `mesh_builder.py` / `map.py`). All linear inputs and outputs are in
millimetres.

A *surface* maps a global XY point (mm) to a Z offset (mm). Each surface is one
self-describing record in the `SURFACES` registry; everything downstream derives
from that single registry:

    * the Blender `EnumProperty` items      -> `enum_items()`
    * the mm-scale feature defaults         -> `feature_mm_default()`
    * the per-point dispatch                -> `surface_offset()`

ADDING A SURFACE (the whole change — one file, ~one function + one line):
    1. Write a generator `def _myfx(x, y, *, feature_mm, depth_mm, regularity,
       seed) -> float` that returns a Z offset bounded by `depth_mm`.
    2. Add one `Surface(...)` record to `SURFACES` pointing at it.
That's it — the enum, the mm defaults, the dispatch, and the parametrised test
suite (which fans out over `SURFACES`) all pick it up automatically. No edits to
`mesh_builder`, `operators`, `properties`, or `panel` are required to register a
new surface; those consumers read the registry.

The offset returned here is the raw texture; `mesh_builder` multiplies it by a
rim-fade factor (using its own `rim_edge_distance`) so shared edges/corners stay
at exact interlock heights. Keeping the fade out of this module is deliberate —
it lets the surfaces stay pure functions of (x, y) with no geometry dependency
(and avoids a circular import, since `mesh_builder` imports this module).
"""

import math

# Real-world man height (mm); model scale = man_height_mm / REAL_MAN_HEIGHT_MM.
REAL_MAN_HEIGHT_MM = 1800.0


# ---------------------------------------------------------------------------
# Deterministic hashing — NO `random`, NO global state. Reproducible rebuilds
# require the same (cell, seed) to always yield the same value.
# ---------------------------------------------------------------------------
def _hash01(ix, iy, seed):
    """Deterministic pseudo-random float in [0.0, 1.0) from integer inputs."""
    h = (ix * 374761393 + iy * 668265263 + seed * 2147483647) & 0xFFFFFFFF
    h = (h ^ (h >> 13)) * 1274126177 & 0xFFFFFFFF
    h ^= h >> 16
    return (h & 0xFFFFFFFF) / 4294967296.0


def _cell_center(ix, iy, pitch, jitter, seed):
    """Jittered centre (mm) of grid cell (ix, iy). jitter in [0,1] = max offset
    as a fraction of a cell; 0 -> perfect grid, 1 -> anywhere in the cell."""
    jx = (_hash01(ix, iy, seed) - 0.5) * jitter
    jy = (_hash01(ix, iy, seed ^ 0x9E3779B9) - 0.5) * jitter
    return ((ix + 0.5 + jx) * pitch, (iy + 0.5 + jy) * pitch)


def _worley(x, y, pitch, jitter, seed):
    """Worley/Voronoi sample. Returns (f1, f2, cell) — distance (mm) to the
    nearest and second-nearest jittered cell centre, and the nearest cell's
    integer coords. Scans the 3x3 neighbourhood, valid while jitter <= 1."""
    cx = int(math.floor(x / pitch))
    cy = int(math.floor(y / pitch))
    f1 = f2 = float("inf")
    nearest = (cx, cy)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            ix, iy = cx + dx, cy + dy
            px, py = _cell_center(ix, iy, pitch, jitter, seed)
            d = math.hypot(x - px, y - py)
            if d < f1:
                f2, f1, nearest = f1, d, (ix, iy)
            elif d < f2:
                f2 = d
    return f1, f2, nearest


def _smoothstep(t):
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return t * t * (3.0 - 2.0 * t)


# ---------------------------------------------------------------------------
# Surface generators. Each returns a Z offset (mm) with |offset| <= depth_mm,
# roughly zero-mean so the texture rides on the macro terrain without lifting it.
# ---------------------------------------------------------------------------
def _cobblestone(x, y, *, feature_mm, depth_mm, regularity, seed, direction_rad=0.0):
    """Rounded Voronoi stones with recessed grout lines. `regularity` (0..1)
    sets cell jitter: low -> neat courses, high -> irregular cobbles.
    Isotropic — ignores `direction_rad`."""
    pitch = max(feature_mm, 1e-6)
    jitter = max(0.0, min(regularity, 1.0))
    f1, f2, _ = _worley(x, y, pitch, jitter, seed)
    # f2 - f1 is ~0 on a cell boundary (grout) and grows toward a cell interior.
    grout_w = 0.22 * pitch
    interior = _smoothstep((f2 - f1) / grout_w)  # 0 at grout, 1 in stone body
    return (interior - 0.5) * depth_mm


def _furrow(x, y, *, feature_mm, depth_mm, regularity, seed, direction_rad=0.0):
    """Parallel plough ridges (directional sine) with a little wander.
    Anisotropic — ridges run ALONG `direction_rad`, so the wave is measured
    across that axis. Structurally unlike the Voronoi surfaces, which is why it
    also exercises the registry's agnosticism to how a generator works."""
    pitch = max(feature_mm, 1e-6)
    c, s = math.cos(direction_rad), math.sin(direction_rad)
    along = c * x + s * y          # distance along the furrows
    across = -s * x + c * y        # distance across them (drives the wave)
    # Low-frequency wander breaks dead-straight rows; `regularity` straightens.
    wander = (_hash01(int(along // (pitch * 8)), 0, seed) - 0.5)
    wander *= (1.0 - max(0.0, min(regularity, 1.0))) * pitch * 0.5
    phase = (across + wander) / pitch * 2.0 * math.pi
    return 0.5 * math.sin(phase) * depth_mm


def _gravel(x, y, *, feature_mm, depth_mm, regularity, seed, direction_rad=0.0):
    """Dense field of small rounded pebbles of varying height. `regularity`
    lowers the size spread (1 -> uniform pebbles, 0 -> very mixed).
    Isotropic — ignores `direction_rad`."""
    pitch = max(feature_mm, 1e-6)
    jitter = 0.55 + 0.45 * (1.0 - max(0.0, min(regularity, 1.0)))
    f1, _, cell = _worley(x, y, pitch, jitter, seed)
    height = _hash01(cell[0], cell[1], seed ^ 0x5BD1E995)  # per-pebble 0..1
    height = (1.0 - regularity) * height + regularity * 0.75
    radius = _smoothstep(1.0 - f1 / (0.5 * pitch))  # 1 at pebble centre, 0 at edge
    return (radius * height - 0.5) * depth_mm


# ---------------------------------------------------------------------------
# Registry — the single source of truth.
# ---------------------------------------------------------------------------
class Surface:
    """One registered procedural surface.

    key            EnumProperty identifier (also the dict key)
    label          UI label
    description    UI tooltip
    reference_mm   real-world feature size (mm); scaled by man-height for the
                   default feature size. 0 for NONE.
    generator      fn(x, y, *, feature_mm, depth_mm, regularity, seed) -> float,
                   or None for the no-op NONE entry.
    default_depth_mm / default_regularity  sensible per-surface starting values.
    """

    def __init__(self, key, label, description, reference_mm, generator,
                 default_depth_mm=2.0, default_regularity=0.5):
        self.key = key
        self.label = label
        self.description = description
        self.reference_mm = reference_mm
        self.generator = generator
        self.default_depth_mm = default_depth_mm
        self.default_regularity = default_regularity


# Insertion order is preserved and drives the enum order. NONE stays first.
SURFACES = {
    s.key: s for s in (
        Surface("NONE", "None", "No procedural surface", 0.0, None),
        Surface("COBBLESTONE", "Cobblestone",
                "Rounded Voronoi stones with recessed grout lines",
                reference_mm=120.0, generator=_cobblestone,
                default_depth_mm=2.5, default_regularity=0.45),
        Surface("GRAVEL", "Gravel",
                "Dense field of small rounded pebbles",
                reference_mm=30.0, generator=_gravel,
                default_depth_mm=1.5, default_regularity=0.3),
        Surface("FURROW", "Plough & Furrow",
                "Parallel ploughed ridges",
                reference_mm=700.0, generator=_furrow,
                default_depth_mm=3.0, default_regularity=0.6),
    )
}


# ---------------------------------------------------------------------------
# Registry-derived public API (consumed by properties / panel / mesh_builder).
# ---------------------------------------------------------------------------
def enum_items():
    """Blender `EnumProperty` items derived from the registry."""
    return [(s.key, s.label, s.description) for s in SURFACES.values()]


def feature_mm_default(surface_type, man_height_mm):
    """Default mm feature size for a surface at the given model scale."""
    surf = SURFACES.get(surface_type)
    if surf is None or surf.reference_mm <= 0.0:
        return 0.0
    return surf.reference_mm * man_height_mm / REAL_MAN_HEIGHT_MM


def surface_offset(x_mm, y_mm, *, surface_type, feature_mm, depth_mm,
                   regularity, seed, origin_xy=(0.0, 0.0), direction_rad=0.0):
    """Raw Z offset (mm) for one point. Samples in GLOBAL coords (local + origin)
    so a pattern flows continuously across tile seams. `direction_rad` orients
    anisotropic surfaces (e.g. furrows); isotropic ones ignore it. Returns 0.0
    for NONE / unknown / non-positive depth or feature size."""
    surf = SURFACES.get(surface_type)
    if surf is None or surf.generator is None:
        return 0.0
    if depth_mm <= 0.0 or feature_mm <= 0.0:
        return 0.0
    gx = x_mm + origin_xy[0]
    gy = y_mm + origin_xy[1]
    return surf.generator(gx, gy, feature_mm=feature_mm, depth_mm=depth_mm,
                          regularity=regularity, seed=int(seed),
                          direction_rad=direction_rad)


# ---------------------------------------------------------------------------
# Region masking — a surface applies only inside a user-drawn polygon (in
# tile-local XY), with a soft falloff band at the boundary. bpy-free + testable;
# defined in continuous XY so it is independent of vertex count and survives
# subdivision/resample changes (the mask is re-evaluated on every rebuild).
# ---------------------------------------------------------------------------
def point_in_polygon(x, y, poly):
    """True if (x, y) is inside polygon `poly` (list of (x, y), implicitly
    closed). Even-odd ray-casting rule."""
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and \
                (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _dist_point_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 1e-18:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / seg2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def polygon_edge_distance(x, y, poly):
    """Shortest distance (mm) from (x, y) to the polygon boundary."""
    n = len(poly)
    best = float("inf")
    j = n - 1
    for i in range(n):
        ax, ay = poly[j]
        bx, by = poly[i]
        d = _dist_point_segment(x, y, ax, ay, bx, by)
        if d < best:
            best = d
        j = i
    return best


def region_mask(x, y, poly, falloff_mm=0.0):
    """Membership weight in [0, 1] for point (x, y) vs region `poly`: 1 well
    inside, ramping smoothly to 0 across a `falloff_mm` band at the boundary, 0
    outside. Degenerate polygons (<3 pts) return 0."""
    if len(poly) < 3:
        return 0.0
    if not point_in_polygon(x, y, poly):
        return 0.0
    if falloff_mm <= 0.0:
        return 1.0
    return _smoothstep(polygon_edge_distance(x, y, poly) / falloff_mm)
