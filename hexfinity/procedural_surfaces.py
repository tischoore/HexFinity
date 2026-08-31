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
def _cobblestone(x, y, *, feature_mm, depth_mm, regularity, seed, direction_rad=0.0, **_):
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


def _furrow(x, y, *, feature_mm, depth_mm, regularity, seed, direction_rad=0.0, **_):
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


def _gravel(x, y, *, feature_mm, depth_mm, regularity, seed, direction_rad=0.0, **_):
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


def _lattice_value(ix, iy, seed):
    """Deterministic pseudo-random float in [-1, 1] for lattice point (ix, iy)."""
    return _hash01(ix, iy, seed) * 2.0 - 1.0


def _value_noise(x, y, pitch, seed):
    """Smooth value noise at wavelength `pitch`: bilinear (smoothstep-eased)
    interpolation of `_lattice_value` between the 4 lattice points
    surrounding (x, y). Continuous and in [-1, 1] everywhere (interpolation of
    values already in that range can't overshoot it)."""
    fx, fy = x / pitch, y / pitch
    ix0, iy0 = int(math.floor(fx)), int(math.floor(fy))
    tx, ty = _smoothstep(fx - ix0), _smoothstep(fy - iy0)
    v00 = _lattice_value(ix0, iy0, seed)
    v10 = _lattice_value(ix0 + 1, iy0, seed)
    v01 = _lattice_value(ix0, iy0 + 1, seed)
    v11 = _lattice_value(ix0 + 1, iy0 + 1, seed)
    a = v00 + (v10 - v00) * tx
    b = v01 + (v11 - v01) * tx
    return a + (b - a) * ty


def _plains(x, y, *, feature_mm, depth_mm, regularity, seed, direction_rad=0.0, **_):
    """Gentle rolling fractal (fBm) value-noise terrain, approximating
    uncultivated grassland/plains. A cheap, dependency-free stand-in for
    Perlin noise, built from the same deterministic hash lattice as the other
    surfaces rather than a gradient-noise library. `feature_mm` is the
    dominant (largest-octave) undulation wavelength; 4 octaves are summed at
    doubling frequency, each weighted by `persistence` raised to its octave
    index and re-normalised so the sum stays in [-1, 1] (bounded by
    `depth_mm`). `regularity` (0..1) sets that persistence
    (`0.65 - 0.45*regularity`): low regularity -> more high-frequency energy
    -> busier, rougher ground; high regularity -> smoother, broader rolling
    hills. Isotropic — ignores `direction_rad`."""
    pitch = max(feature_mm, 1e-6)
    persistence = 0.65 - 0.45 * max(0.0, min(regularity, 1.0))
    amp = 1.0
    freq = 1.0
    total = 0.0
    norm = 0.0
    for octave in range(4):
        total += amp * _value_noise(x * freq, y * freq, pitch,
                                    seed ^ (octave * 0x1000193))
        norm += amp
        amp *= persistence
        freq *= 2.0
    return (total / norm) * depth_mm


def _lake_water(x, y, *, feature_mm, depth_mm, regularity, seed, direction_rad=0.0, **_):
    """Calm standing water: interference of several plane waves at different
    angles/phases (a lightweight stand-in for a Gerstner-wave sum). `regularity`
    (0..1) narrows the angle/frequency spread: 1 -> evenly-spaced parallel swell,
    0 -> chaotic wind-chopped interference. Isotropic — ignores `direction_rad`."""
    pitch = max(feature_mm, 1e-6)
    k = 2.0 * math.pi / pitch
    n = 5
    chop = 1.0 - max(0.0, min(regularity, 1.0))
    total = 0.0
    weight_sum = 0.0
    for i in range(n):
        base_angle = i * math.pi / n
        angle = base_angle + (_hash01(i, 0, seed) - 0.5) * chop * math.pi
        freq = 1.0 + (_hash01(i, 1, seed) - 0.5) * chop
        phase = _hash01(i, 2, seed) * 2.0 * math.pi
        weight = 1.0 / (i + 1)
        c, s = math.cos(angle), math.sin(angle)
        total += weight * math.sin((c * x + s * y) * k * freq + phase)
        weight_sum += weight
    return (total / weight_sum) * depth_mm


def _river_water(x, y, *, feature_mm, depth_mm, regularity, seed, direction_rad=0.0, **_):
    """Flowing water in a wide channel: cross-flow ripple bands (like Furrow)
    blended with elongated current streaks (Worley cells stretched along the
    flow). `regularity` straightens the ripple wander, same convention as
    Furrow. Anisotropic — ridges/streaks run ALONG `direction_rad`."""
    pitch = max(feature_mm, 1e-6)
    c, s = math.cos(direction_rad), math.sin(direction_rad)
    along = c * x + s * y
    across = -s * x + c * y
    reg = max(0.0, min(regularity, 1.0))

    wander = (_hash01(int(along // (pitch * 6)), 0, seed) - 0.5)
    wander *= (1.0 - reg) * pitch * 0.6
    ripple = math.sin((across + wander) / pitch * 2.0 * math.pi)

    stretch = 4.0
    f1, f2, _ = _worley(along / stretch, across, pitch, 0.6, seed ^ 0x517CC1B7)
    streak = 2.0 * _smoothstep((f2 - f1) / (0.3 * pitch)) - 1.0

    return (0.65 * ripple + 0.35 * streak) * depth_mm


def _creek_water(x, y, *, feature_mm, depth_mm, regularity, seed, direction_rad=0.0, **_):
    """Narrow, turbulent flowing water: fractal value-noise (as Plains) evaluated
    in the flow-aligned frame with the along-flow frequency compressed, so
    turbulence streaks trail downstream instead of scattering isotropically.
    `regularity` sets octave persistence, same convention as Plains.
    Anisotropic — turbulence elongates ALONG `direction_rad`."""
    pitch = max(feature_mm, 1e-6)
    c, s = math.cos(direction_rad), math.sin(direction_rad)
    along = c * x + s * y
    across = -s * x + c * y
    persistence = 0.65 - 0.45 * max(0.0, min(regularity, 1.0))
    elongation = 3.0
    amp = 1.0
    freq = 1.0
    total = 0.0
    norm = 0.0
    for octave in range(4):
        total += amp * _value_noise(along * freq / elongation, across * freq,
                                    pitch, seed ^ (octave * 0x1000193))
        norm += amp
        amp *= persistence
        freq *= 2.0
    return (total / norm) * depth_mm


# ---------------------------------------------------------------------------
# Scatter surfaces — a SECOND fundamental kind of parametric surface. Where a
# `displace` surface returns a per-point Z offset (above), a `scatter` surface
# places DISTINCT objects in the region and leaves the tile surface untouched.
#
# Everything below is still pure geometry math (no bpy): a placement planner, a
# per-element mesh generator, and an assembler that joins the elements into one
# (verts, faces) mesh. The bpy shell (`scatter.py`) only adds object lifecycle,
# the raycast that seats each element on the real terrain, and the merge boolean.
# ---------------------------------------------------------------------------
def _clamp01(t):
    return 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)


def hex_polygon(diameter_mm):
    """Flat-top hexagon outline (list of (x, y) tile-local mm) matching the rim
    corners P1..P6 built by `mesh_builder.build_hex_tile`. Used as the implicit
    polygon for a whole-tile scatter region (no drawn loop)."""
    R = diameter_mm / 2.0
    pts = []
    for i in range(6):
        angle = math.pi / 3.0 - i * (math.pi / 3.0)
        pts.append((R * math.cos(angle), R * math.sin(angle)))
    return pts


def _normalize(v):
    x, y, z = v
    m = math.sqrt(x * x + y * y + z * z)
    if m <= 1e-18:
        return (0.0, 0.0, 0.0)
    return (x / m, y / m, z / m)


def _icosahedron():
    """12-vertex / 20-face regular icosahedron on the unit sphere."""
    t = (1.0 + math.sqrt(5.0)) / 2.0
    verts = [
        (-1.0,  t,  0.0), (1.0,  t,  0.0), (-1.0, -t,  0.0), (1.0, -t,  0.0),
        (0.0, -1.0,  t), (0.0,  1.0,  t), (0.0, -1.0, -t), (0.0,  1.0, -t),
        (t,  0.0, -1.0), (t,  0.0,  1.0), (-t,  0.0, -1.0), (-t,  0.0,  1.0),
    ]
    faces = [
        (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
        (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
        (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
        (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1),
    ]
    return [_normalize(v) for v in verts], faces


def _icosphere(subdiv):
    """Geodesic icosphere (unit radius) from `subdiv` edge-subdivision passes.
    A shared midpoint cache keeps every edge between exactly two faces, so the
    result stays a closed 2-manifold (validated by the test suite)."""
    verts, faces = _icosahedron()
    verts = [list(v) for v in verts]
    for _ in range(max(0, int(subdiv))):
        cache = {}
        new_faces = []

        def midpoint(a, b):
            key = (a, b) if a < b else (b, a)
            idx = cache.get(key)
            if idx is not None:
                return idx
            va, vb = verts[a], verts[b]
            m = _normalize(((va[0] + vb[0]) * 0.5,
                            (va[1] + vb[1]) * 0.5,
                            (va[2] + vb[2]) * 0.5))
            verts.append(list(m))
            idx = len(verts) - 1
            cache[key] = idx
            return idx

        for (a, b, c) in faces:
            ab = midpoint(a, b)
            bc = midpoint(b, c)
            ca = midpoint(c, a)
            new_faces.extend([(a, ab, ca), (b, bc, ab),
                              (c, ca, bc), (ab, bc, ca)])
        faces = new_faces
    return [tuple(v) for v in verts], faces


def boulder_mesh(radius_mm, pid, *, roughness=0.35, subdiv=1):
    """One boulder: an icosphere with per-vertex RADIAL noise so it looks like a
    rough rock rather than a ball. Each unit vertex is scaled by
    `radius_mm * (1 ± roughness/2)`; because every vertex moves only along its
    own radius the topology (and hence 2-manifoldness) is preserved. The noise is
    deterministic from `pid`, so a given placement rebuilds identically and two
    boulders with different `pid` differ."""
    unit_verts, faces = _icosphere(subdiv)
    rough = max(0.0, float(roughness))
    out = []
    for i, (x, y, z) in enumerate(unit_verts):
        n = _hash01(i, int(pid), 0x5EED2A17)
        scale = radius_mm * (1.0 + rough * (n - 0.5))
        out.append((x * scale, y * scale, z * scale))
    return out, faces


def scatter_boulders(polygon, *, min_size_mm, max_size_mm, density,
                     distribution, seed):
    """Plan boulder placements inside `polygon` (tile-local mm). Returns a list of
    `(x_mm, y_mm, radius_mm, rot_rad, pid)` tuples — Z is resolved later by the
    caller's raycast. Deterministic from `seed` so rebuilds are stable.

    A jittered grid drives placement: the cell pitch and a per-cell presence
    probability both tighten with `density` (so more density -> more boulders).
    Each kept cell gets a radius drawn from `[min,max]` and shaped by
    `distribution` (0 = many small + few large, 1 = uniform) plus a random
    rotation. `min/max_size_mm` are DIAMETERS; the stored radius is half."""
    if len(polygon) < 3:
        return []
    lo = max(0.0, min(min_size_mm, max_size_mm))
    hi = max(0.0, max(min_size_mm, max_size_mm))
    mean_size = 0.5 * (lo + hi)
    if mean_size <= 0.0:
        return []
    density = _clamp01(density)
    distribution = _clamp01(distribution)
    seed = int(seed)

    # density 1 -> pitch == mean size (packed); density 0 -> 4x (sparse).
    pitch = mean_size * (1.0 + (1.0 - density) * 3.0)
    # density 0 -> 40% of cells occupied; density 1 -> all occupied.
    presence = 0.4 + 0.6 * density
    # distribution 1 -> uniform (k=1); 0 -> strongly small-biased (k=4).
    k = 1.0 + (1.0 - distribution) * 3.0

    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    ix0 = int(math.floor(min(xs) / pitch))
    ix1 = int(math.floor(max(xs) / pitch))
    iy0 = int(math.floor(min(ys) / pitch))
    iy1 = int(math.floor(max(ys) / pitch))

    placements = []
    for iy in range(iy0, iy1 + 1):
        for ix in range(ix0, ix1 + 1):
            if _hash01(ix, iy, seed ^ 0x0A53F00D) >= presence:
                continue
            cx, cy = _cell_center(ix, iy, pitch, 0.8, seed)
            if not point_in_polygon(cx, cy, polygon):
                continue
            u = _hash01(ix, iy, seed ^ 0x1234ABCD)
            size = lo + (hi - lo) * (u ** k)
            rot = _hash01(ix, iy, seed ^ 0x0BEEF123) * 2.0 * math.pi
            pid = ((ix * 73856093) ^ (iy * 19349663) ^ seed) & 0x7FFFFFFF
            placements.append((cx, cy, 0.5 * size, rot, pid))
    return placements


def polygon_area(poly):
    """Unsigned area (mm²) of polygon `poly` via the shoelace formula."""
    n = len(poly)
    if n < 3:
        return 0.0
    acc = 0.0
    j = n - 1
    for i in range(n):
        acc += (poly[j][0] + poly[i][0]) * (poly[j][1] - poly[i][1])
        j = i
    return abs(acc) * 0.5


def estimate_boulder_count(polygon, *, min_size_mm, max_size_mm, density):
    """Rough expected boulder count for a scatter region — cheap enough for a
    live panel warning. Mirrors `scatter_boulders`' grid: cells ≈ area / pitch²,
    thinned by the per-cell presence probability. Approximate (no per-cell
    inside-polygon test), so it's an estimate, not a guarantee."""
    area = polygon_area(polygon)
    mean_size = 0.5 * (max(0.0, min_size_mm) + max(0.0, max_size_mm))
    if area <= 0.0 or mean_size <= 0.0:
        return 0
    density = _clamp01(density)
    pitch = mean_size * (1.0 + (1.0 - density) * 3.0)
    presence = 0.4 + 0.6 * density
    return int(area / (pitch * pitch) * presence)


def assemble_scatter_mesh(placements, z_of, *, sink_mm=1.0, roughness=0.35,
                          subdiv=1):
    """Join every boulder in `placements` into ONE `(verts, faces)` mesh.

    For each placement the boulder mesh is rotated about Z, translated to the
    placement XY, and seated in Z so its centre sits at `z_of(x, y) + radius -
    sink_mm` — i.e. it rests on the terrain sampled under it with a small
    `sink_mm` overlap (so a later boolean merge stays manifold). `z_of` is
    injected by the caller (a bpy raycast in the shell) so this stays bpy-free
    and unit-testable. The boulders share no vertices, so the concatenation of
    closed 2-manifold boulders is itself 2-manifold."""
    all_verts = []
    all_faces = []
    for (x, y, radius, rot, pid) in placements:
        bverts, bfaces = boulder_mesh(radius, pid, roughness=roughness,
                                      subdiv=subdiv)
        base = len(all_verts)
        c, s = math.cos(rot), math.sin(rot)
        z0 = z_of(x, y) + radius - sink_mm
        for (vx, vy, vz) in bverts:
            rx = c * vx - s * vy
            ry = s * vx + c * vy
            all_verts.append((rx + x, ry + y, vz + z0))
        for f in bfaces:
            all_faces.append(tuple(base + idx for idx in f))
    return all_verts, all_faces


# ---------------------------------------------------------------------------
# Registry — the single source of truth.
# ---------------------------------------------------------------------------
class ParamSpec:
    """One extra (surface-specific) parameter, e.g. a scatter surface's
    "Min Boulder Size". Carries everything the UI and the marshaller need:

    key            attribute name used in the marshalled region dict
    label / description   UI text
    default        plain default (used directly for `unit='factor'`)
    min / max      slider bounds
    unit           'mm' (scales with man-height from `reference_mm`) or 'factor'
    reference_mm   real-world size (mm) for an 'mm' param, scaled like a feature
    """

    def __init__(self, key, label, description, default, min, max, *,
                 unit='factor', reference_mm=0.0):
        self.key = key
        self.label = label
        self.description = description
        self.default = default
        self.min = min
        self.max = max
        self.unit = unit
        self.reference_mm = reference_mm

    def scaled_default(self, man_height_mm):
        """Starting value at the given model scale: an 'mm' param scales its
        `reference_mm` by man-height (like a feature size); otherwise the plain
        default."""
        if self.unit == 'mm' and self.reference_mm > 0.0:
            return self.reference_mm * man_height_mm / REAL_MAN_HEIGHT_MM
        return self.default


class Surface:
    """One registered procedural surface — of one of two *kinds*:

    kind == 'displace' (default): a heightfield. `generator(x, y, *, feature_mm,
        depth_mm, regularity, seed, direction_rad) -> float` returns a Z offset
        bounded by `depth_mm`; `surface_offset()` dispatches to it.
    kind == 'scatter': places DISTINCT objects and leaves the surface flat.
        `generator is None` (so it contributes ZERO displacement and is excluded
        from the displacement test fan-out automatically); instead it carries
        `placement_fn` + `element_mesh_fn` and a tuple of `extra_params`
        (ParamSpec) consumed by the bpy shell `scatter.py`.

    key            EnumProperty identifier (also the dict key)
    label          UI label
    description    UI tooltip
    reference_mm   real-world feature size (mm); scaled by man-height for the
                   default feature size. 0 for NONE / scatter.
    generator      displace fn, or None (NONE entry and all scatter surfaces).
    default_depth_mm / default_regularity  sensible per-surface starting values.
    uses_feature / uses_direction  whether the panel shows the Feature Size /
                   Direction rows for this surface.
    extra_params   tuple of ParamSpec — the surface-specific knobs (scatter).
    """

    def __init__(self, key, label, description, reference_mm, generator,
                 default_depth_mm=2.0, default_regularity=0.5, *,
                 kind='displace', placement_fn=None, element_mesh_fn=None,
                 uses_feature=True, uses_direction=False, extra_params=()):
        self.key = key
        self.label = label
        self.description = description
        self.reference_mm = reference_mm
        self.generator = generator
        self.default_depth_mm = default_depth_mm
        self.default_regularity = default_regularity
        self.kind = kind
        self.placement_fn = placement_fn
        self.element_mesh_fn = element_mesh_fn
        self.uses_feature = uses_feature
        self.uses_direction = uses_direction
        self.extra_params = tuple(extra_params)


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
                default_depth_mm=3.0, default_regularity=0.6,
                uses_direction=True),
        Surface("PLAINS", "Uncultivated Plains",
                "Gentle rolling fractal noise for natural grassland/plains ground",
                reference_mm=900.0, generator=_plains,
                default_depth_mm=2.0, default_regularity=0.4),
        Surface("LAKE_WATER", "Lake / Still Water",
                "Gentle interference ripples for calm standing water",
                reference_mm=150.0, generator=_lake_water,
                default_depth_mm=1.0, default_regularity=0.7),
        Surface("RIVER_WATER", "River / Flowing Water",
                "Directional current ripples with elongated streak texture",
                reference_mm=300.0, generator=_river_water,
                default_depth_mm=1.5, default_regularity=0.5,
                uses_direction=True),
        Surface("CREEK_WATER", "Creek / Stream",
                "Narrow, turbulent flowing water — choppier than a river",
                reference_mm=100.0, generator=_creek_water,
                default_depth_mm=1.2, default_regularity=0.35,
                uses_direction=True),
        Surface("BOULDERS", "Boulder Field",
                "Scattered boulder objects across a range of sizes "
                "(does not change the tile surface)",
                reference_mm=0.0, generator=None,
                kind='scatter', placement_fn=scatter_boulders,
                element_mesh_fn=boulder_mesh, uses_feature=False,
                extra_params=(
                    ParamSpec("min_size_mm", "Min Boulder Size (mm)",
                              "Diameter of the smallest boulders",
                              8.0, 0.5, 200.0, unit='mm', reference_mm=80.0),
                    ParamSpec("max_size_mm", "Max Boulder Size (mm)",
                              "Diameter of the largest boulders",
                              40.0, 0.5, 500.0, unit='mm', reference_mm=400.0),
                    ParamSpec("density", "Boulder Density",
                              "0 = sparse scatter, 1 = packed field",
                              0.6, 0.0, 1.0, unit='factor'),
                    ParamSpec("distribution", "Size Distribution",
                              "0 = many small + few large, 1 = uniform sizes",
                              0.3, 0.0, 1.0, unit='factor'),
                )),
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


def surface_kind(surface_type):
    """The kind ('displace' / 'scatter') of a registered surface; 'displace' for
    unknown keys (the safe default)."""
    surf = SURFACES.get(surface_type)
    return surf.kind if surf is not None else 'displace'


def extra_param_specs(surface_type):
    """Tuple of ParamSpec for a surface's extra (surface-specific) params."""
    surf = SURFACES.get(surface_type)
    return surf.extra_params if surf is not None else ()


def extra_param_defaults(surface_type, man_height_mm):
    """Mapping {param key -> starting value} for a surface's extra params at the
    given model scale (mm params scale with man-height)."""
    return {p.key: p.scaled_default(man_height_mm)
            for p in extra_param_specs(surface_type)}


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


# ---------------------------------------------------------------------------
# Oriented-bounding-box overlap — used by flora.py's plant-time collision
# check so two trees' rotated footprints (each tree is randomly spun around
# its vertical axis at plant time) can be tested for intersection without
# ever needing bpy. bpy-free + testable.
# ---------------------------------------------------------------------------
def obb_overlap(cx1, cy1, hx1, hy1, angle1,
                 cx2, cy2, hx2, hy2, angle2, min_gap=0.0):
    """True if two 2D oriented rectangles overlap.

    Each rectangle is given by its center `(cx, cy)`, half-extents
    `(hx, hy)` along its own local X/Y axes, and `angle` (radians) that its
    local axes are rotated by around Z. Tested via the separating-axis
    theorem over the 4 face-normal axes (the local X/Y axes of each
    rectangle) — the standard sufficient test for 2D OBB/OBB overlap; unlike
    3D OBBs, no extra cross-product axes are needed.

    `min_gap` inflates both rectangles' half-extents by `min_gap / 2` before
    the test, so a nonzero gap requires that much clearance between the
    rectangles' *surfaces*, not just their centers. Rectangles that only
    touch (zero-width gap) do NOT count as overlapping.
    """
    pad = min_gap / 2.0
    hx1, hy1 = hx1 + pad, hy1 + pad
    hx2, hy2 = hx2 + pad, hy2 + pad

    ax1, ay1 = math.cos(angle1), math.sin(angle1)   # box 1's local X axis
    bx1, by1 = -ay1, ax1                              # box 1's local Y axis
    ax2, ay2 = math.cos(angle2), math.sin(angle2)   # box 2's local X axis
    bx2, by2 = -ay2, ax2                              # box 2's local Y axis

    tx, ty = cx2 - cx1, cy2 - cy1

    for axis_x, axis_y in ((ax1, ay1), (bx1, by1), (ax2, ay2), (bx2, by2)):
        dist = abs(tx * axis_x + ty * axis_y)
        radius1 = hx1 * abs(ax1 * axis_x + ay1 * axis_y) + \
            hy1 * abs(bx1 * axis_x + by1 * axis_y)
        radius2 = hx2 * abs(ax2 * axis_x + ay2 * axis_y) + \
            hy2 * abs(bx2 * axis_x + by2 * axis_y)
        if dist >= radius1 + radius2:
            return False   # this axis separates the two boxes
    return True
