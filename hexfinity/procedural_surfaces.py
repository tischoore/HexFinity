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

try:
    from . import segment_geometry as sg
except ImportError:
    import segment_geometry as sg

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


def _signed_area(poly):
    """Signed shoelace area -- positive for CCW winding, negative for CW.
    Used by the flagstone tessellation to normalise an arbitrary (hand-drawn
    or hex) region polygon to CCW before treating its edges as inward-facing
    half-plane constraints."""
    n = len(poly)
    if n < 3:
        return 0.0
    acc = 0.0
    j = n - 1
    for i in range(n):
        acc += (poly[j][0] + poly[i][0]) * (poly[j][1] - poly[i][1])
        j = i
    return -acc * 0.5


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
# Flagstone Paving — a second `scatter` surface. Where a boulder is a rough,
# freely-tumbled rock seated flat at a single centre height, a flagstone is a
# flat paving slab that must rest flush against the (possibly sloped) local
# terrain, like a real paver on uneven ground. That needs a per-element TILT
# fit, not just a height — see assemble_flagstone_mesh below.
# ---------------------------------------------------------------------------
def flagstone_mesh(outline_local, thickness_mm):
    """One paving slab: a flat-topped prism built directly from
    `outline_local` (a CCW list of `(x, y)` tile-local-relative-to-the-
    stone's-own-centre points — its own tessellation-derived footprint, see
    `scatter_flagstones`). The outline is duplicated as a flat BOTTOM ring
    (z=0 — the face `assemble_flagstone_mesh` seats on the terrain) and a
    flat TOP ring (z=+thickness_mm), closed by one side quad per edge plus a
    bottom/top n-gon cap (reversed winding on the bottom so its outward
    normal faces -Z). Unlike the old generated-n-gon version, vertex/face
    count (`2*n`/`n+2`) is NOT fixed across stones — each stone's outline is
    its own inset tessellation cell, a different shape and vertex count from
    its neighbours by construction."""
    n = len(outline_local)
    bottom = [(x, y, 0.0) for (x, y) in outline_local]
    top = [(x, y, thickness_mm) for (x, y) in outline_local]
    verts = bottom + top

    faces = [tuple(reversed(range(n))), tuple(n + i for i in range(n))]
    for i in range(n):
        j = (i + 1) % n
        faces.append((i, j, n + j, n + i))
    return verts, faces


# ---------------------------------------------------------------------------
# Flagstone placement: a power diagram (weighted Voronoi) tessellation of the
# region, each cell inset along its shared edges by a jittered half of the
# target mortar gap. A tessellation covers the whole region by construction
# (no leftover holes, unlike a packer that can leave uncovered pockets
# between separately-grown fronts), and because each shared edge is inset
# independently from BOTH sides, the realized gap on that edge is the SUM of
# two independent jittered half-gaps — which averages to `gap_mm` by
# linearity even though any one edge may come out narrower or wider, or (once
# an inset clamps to the cell's own size) touch at exactly 0.
# ---------------------------------------------------------------------------
def _intersect_halfplanes(subject, halfplanes):
    """Clip polygon `subject` (list of `(x, y)`, CCW; may be concave) against
    every halfplane in `halfplanes` — `(a, b, c, tag)` tuples meaning "keep
    points with `a*x + b*y <= c`" — applied one at a time via
    Sutherland-Hodgman. Each edge of the result is tagged: an edge inherited
    unsplit from `subject` keeps `None`; an edge newly introduced by clipping
    against halfplane k gets that halfplane's own `tag`. Returns `(verts,
    edge_tags)` where `edge_tags[i]` is the tag of the edge from `verts[i]`
    to `verts[(i+1) % n]`; returns `([], [])` once the polygon clips away to
    nothing.

    Correct for a concave `subject` against any SEQUENCE of individually
    convex (single-constraint) halfplanes — each clip step is a standard
    convex-window Sutherland-Hodgman clip, valid regardless of the subject's
    own convexity. It is NOT a general polygon-offset routine: feeding a
    polygon's OWN edges back in as `halfplanes` only reproduces an inward
    offset of that exact polygon when the polygon is convex (every cell this
    module builds from a convex boundary is convex; see `scatter_flagstones`
    for the one documented exception)."""
    verts = list(subject)
    tags = [None] * len(verts)
    eps = 1e-9
    for (a, b, c, tag) in halfplanes:
        if not verts:
            break
        n = len(verts)
        new_v, new_t = [], []
        for i in range(n):
            cur, nxt = verts[i], verts[(i + 1) % n]
            cur_tag = tags[i]
            cur_val = a * cur[0] + b * cur[1]
            nxt_val = a * nxt[0] + b * nxt[1]
            cur_in = cur_val <= c + eps
            nxt_in = nxt_val <= c + eps
            if cur_in:
                new_v.append(cur)
                new_t.append(cur_tag)
            if cur_in != nxt_in:
                denom = nxt_val - cur_val
                t = 0.5 if abs(denom) < 1e-15 else (c - cur_val) / denom
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                point = (cur[0] + t * (nxt[0] - cur[0]),
                         cur[1] + t * (nxt[1] - cur[1]))
                new_v.append(point)
                new_t.append(tag if cur_in else cur_tag)
        verts, tags = new_v, new_t
    return verts, tags


def _dedupe_polygon_vertices(verts, tags, min_edge_mm):
    """Merge away a near-duplicate vertex following a VERY short edge (one
    under `min_edge_mm`) — the artifact left behind when two different clip
    lines (from `_intersect_halfplanes` being applied one halfplane at a
    time) happen to cross the polygon boundary at nearly the same point.
    Geometrically harmless on its own (the polygon's shape barely changes),
    but a surviving sliver edge becomes a razor-thin quad in the final
    mesh — invisible at a glance, but a real artifact a close look (or a
    slicer) would notice. Keeps the tag of the LONGER edge that follows the
    merge, since that's the one that actually mattered; a discarded sliver's
    own tag is never the only record of a mortar seam (the neighbour across
    it carries the same seam on its own, non-degenerate edge too). Caps
    total work at one pass around the polygon regardless of how many
    consecutive short edges it finds."""
    n = len(verts)
    if n <= 3:
        return verts, tags
    out_v, out_t = [], []
    i = 0
    visited = 0
    while visited < n:
        out_v.append(verts[i])
        j = (i + 1) % n
        hops = 1
        while hops < n and math.hypot(verts[j][0] - verts[i][0],
                                      verts[j][1] - verts[i][1]) < min_edge_mm:
            i = j
            j = (i + 1) % n
            hops += 1
            visited += 1
        out_t.append(tags[i])
        visited += 1
        i = j
    if len(out_v) < 3:
        return [], []
    return out_v, out_t


# Safety cap on how many times `_power_cell` is allowed to widen its
# neighbour search before giving up and returning its best effort — in
# practice 1-2 widenings cover any realistic seed layout (see that
# function's own docstring for why a fixed, un-widened search radius isn't
# safe on its own).
_POWER_CELL_MAX_RINGS = 8


def _power_cell(i, seeds, boundary, grid, bucket_size, weight_span, min_edge_mm):
    """The power-diagram cell of seed `i` (an index into `seeds`, a list of
    `(x, y, weight)`), clipped to `boundary` (CCW). `weight = (size/2)**2`
    per seed is what makes a bigger-weighted seed claim more area than a
    smaller one — the power/Laguerre bisector between sites `i`/`j` is still
    a straight line (unlike multiplicative weighting, which bends it into an
    arc), so the result stays a polygon, found the same way as a plain
    Voronoi cell: intersect `boundary` against the "closer to i than j"
    halfplane of every nearby seed `j`, then `_dedupe_polygon_vertices` (with
    `min_edge_mm`) to merge away any near-duplicate vertex the sequential
    clipping left behind. Returns `(verts, edge_tags)` — an edge tagged
    `None` came from `boundary` (the paved area's own rim, never inset for
    mortar); an edge tagged `j` is the shared mortar seam with seed `j`.

    `grid`/`bucket_size` are the spatial hash built once by
    `scatter_flagstones`, keyed by `bucket_size`-sized cells. A fixed,
    un-widened neighbour search (e.g. a single 3x3 ring) is NOT safe here:
    random seed jitter routinely leaves a locally sparser-than-average patch
    somewhere, and the one seed that should have clipped `i`'s cell in that
    direction can end up just outside a fixed search radius — leaving that
    whole side of the cell unconstrained, so it balloons out to (or toward)
    the far side of `boundary` instead of stopping at its true, slightly
    farther neighbour. (This was a real, reproducible bug: one oversized
    stone, many times the area of its neighbours, floating above an
    otherwise-correct tessellation.) So the ring radius starts small and
    widens whenever it might still be missing something relevant: after each
    clip, `D` is the farthest the resulting cell actually reaches from seed
    `i`; a not-yet-considered seed at distance `d` has its power bisector at
    signed distance (from `i`, toward it) of at least `d/2 - weight_span /
    (2*d)` in the worst case (`weight_span` the largest possible
    `|weight_i - weight_j|` over the whole `min/max_size_mm` range) — solving
    for where that lower bound first reaches `D` gives the search radius
    that's provably wide enough no unconsidered seed could still cut the
    cell any closer — each widening jumps straight to that radius rather
    than growing by one ring at a time, so it converges in 1-2 passes for
    any realistic layout even when the first (cheap, small) guess was way
    too small; `_POWER_CELL_MAX_RINGS` just bounds the pathological case."""
    xi, yi, wi = seeds[i]
    cx = int(math.floor(xi / bucket_size))
    cy = int(math.floor(yi / bucket_size))
    halfplanes = []
    considered = set()
    k = 1
    cell_verts, cell_tags = list(boundary), [None] * len(boundary)
    for _ in range(_POWER_CELL_MAX_RINGS):
        for dy in range(-k, k + 1):
            for dx in range(-k, k + 1):
                for j in grid.get((cx + dx, cy + dy), ()):
                    if j == i or j in considered:
                        continue
                    considered.add(j)
                    xj, yj, wj = seeds[j]
                    a, b = xj - xi, yj - yi
                    if a == 0.0 and b == 0.0:
                        continue
                    c = 0.5 * ((xj * xj + yj * yj) - (xi * xi + yi * yi) + wi - wj)
                    halfplanes.append((a, b, c, j))
        cell_verts, cell_tags = _intersect_halfplanes(boundary, halfplanes)
        if not cell_verts:
            return cell_verts, cell_tags
        d_max = max(math.hypot(vx - xi, vy - yi) for (vx, vy) in cell_verts)
        covered = k * bucket_size
        needed = d_max + math.sqrt(d_max * d_max + weight_span)
        if needed <= covered:
            return _dedupe_polygon_vertices(cell_verts, cell_tags, min_edge_mm)
        # Jump straight to the ring that should cover `needed` rather than
        # widening by one each time -- converges in one extra pass even when
        # the first (tiny, cheap) guess was far too small.
        k = max(k + 1, int(math.ceil(needed / bucket_size)))
    return _dedupe_polygon_vertices(cell_verts, cell_tags, min_edge_mm)


def _inset_cell(cell_verts, cell_tags, gap_mm, seed_i, jitter_seed):
    """Shrink convex polygon `cell_verts` (CCW, from `_power_cell`) by
    insetting each edge tagged with a neighbour index `j` inward by its own
    independently-jittered half of `gap_mm` (mean `gap_mm/2`, so two
    neighbouring cells' insets sum to `gap_mm` on average across the whole
    tessellation); an edge tagged `None` (the paved area's own rim) is never
    inset. `jitter_seed` keyed by the unordered `(seed_i, j)` pair so both
    sides of one shared edge draw from the SAME hash call — this is what
    makes the inset the two cells apply to their shared edge independent
    draws from each side's own perspective while still letting a caller
    reconstruct (for a test) what either side used. Degenerate/too-tight
    insets are clamped (never negative) by `_intersect_halfplanes` itself —
    an inset that would collapse the polygon simply clips it down toward a
    point rather than self-intersecting."""
    n = len(cell_verts)
    if n < 3:
        return cell_verts
    halfplanes = []
    for k in range(n):
        ax, ay = cell_verts[k]
        bx, by = cell_verts[(k + 1) % n]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        # Outward unit normal of a CCW edge: rotate the edge direction -90°.
        nx, ny = dy / length, -dx / length
        c = nx * ax + ny * ay
        tag = cell_tags[k]
        if tag is None:
            inset = 0.0
        else:
            lo_i, hi_i = (seed_i, tag) if seed_i < tag else (tag, seed_i)
            jitter = 0.1 + 1.8 * _hash01(lo_i, hi_i, jitter_seed)
            inset = max(0.0, gap_mm * 0.5 * jitter)
        halfplanes.append((nx, ny, c - inset, tag))
    verts, _tags = _intersect_halfplanes(cell_verts, halfplanes)
    return verts


# Safety ceiling so a pathological parameter combo (near-zero stone size
# over a huge region) can't make scatter_flagstones run away — mirrors the
# MAX_LEVELS-style caps elsewhere in this codebase (e.g. tree_pads.py).
_FLAGSTONE_MAX_SEEDS = 4000

# Rough coverage fudge factor for estimate_flagstone_count ONLY — the real
# tessellation covers very close to the full region (minus the comparatively
# small mortar-joint area), unlike the old packer's much looser efficiency.
_FLAGSTONE_COVERAGE_FACTOR = 0.95

# A placement whose final (inset) cell area falls below this fraction of its
# own seed's nominal circular area (pi * weight) is dropped as a sliver
# rather than kept as an unusably tiny/oddly-shaped "stone" -- see
# scatter_flagstones' own placement loop for why this matters in practice
# (a tight/acute region-boundary corner, from a hand-drawn or flood-filled
# region, is the common trigger).
_FLAGSTONE_MIN_CELL_AREA_FRACTION = 0.2


# Miter joins sharper than this (miter length / half-width) are treated as
# "too tight" — the single offset outline would spike or self-intersect, so
# `path_corridor_polygons` falls back to one quad per polyline segment.
_CORRIDOR_MITER_LIMIT = 4.0


def _segments_cross(p1, p2, p3, p4):
    """True if open segments p1-p2 and p3-p4 properly intersect."""
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    return ((d1 > 1e-12 and d2 < -1e-12) or (d1 < -1e-12 and d2 > 1e-12)) and \
           ((d3 > 1e-12 and d4 < -1e-12) or (d3 < -1e-12 and d4 > 1e-12))


def _polygon_is_simple(poly):
    n = len(poly)
    for i in range(n):
        a1, a2 = poly[i], poly[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue   # adjacent via the closing edge
            if _segments_cross(a1, a2, poly[j], poly[(j + 1) % n]):
                return False
    return True


def _signed_area(poly):
    n = len(poly)
    return 0.5 * sum(poly[i][0] * poly[(i + 1) % n][1]
                     - poly[(i + 1) % n][0] * poly[i][1] for i in range(n))


def _clip_to_hex(poly, diameter_mm):
    """Clip `poly` to the tile hexagon via `_intersect_halfplanes`."""
    hexp = hex_polygon(diameter_mm)
    halfplanes = []
    for i in range(6):
        (x0, y0), (x1, y1) = hexp[i], hexp[(i + 1) % 6]
        a, b = (y1 - y0), -(x1 - x0)
        c = a * x0 + b * y0
        if c < 0.0:            # orient so the hex centre (origin) is kept
            a, b, c = -a, -b, -c
        halfplanes.append((a, b, c, None))
    verts, _ = _intersect_halfplanes(poly, halfplanes)
    return verts


def _nearest_hex_edge(x, y, diameter_mm):
    """(distance, outward_nx, outward_ny) to the nearest edge LINE of the
    tile hexagon (`hex_polygon`) — 0 on an edge, the apothem at the centre.
    A local copy of `mesh_builder.rim_edge_distance`'s math (that module
    imports this one, so it can't be imported back)."""
    hexp = hex_polygon(diameter_mm)
    best = None
    for i in range(6):
        (x0, y0), (x1, y1) = hexp[i], hexp[(i + 1) % 6]
        mx, my = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        m = math.hypot(mx, my)
        nx, ny = mx / m, my / m          # outward normal (hex centred on origin)
        dist = abs(m - (nx * x + ny * y))
        if best is None or dist < best[0]:
            best = (dist, nx, ny)
    return best


def extend_rim_endpoints(points, width_mm, diameter_mm, tol_mm=None):
    """Copy of `points` where an END point (first or last) lying on the hex
    rim is pushed outward along its own end segment's direction, past the
    rim by `width_mm` plus a margin. Buffering the extended line and then
    clipping to the hex (`path_corridor_polygons`) cuts the corridor end
    exactly along the hex edge — or both edges at a P corner — instead of
    leaving a cap perpendicular to the last segment, which is skewed
    against the edge whenever the line doesn't meet it at 90°. Off-rim ends,
    mid-line points, and an end segment that runs inward from the rim are
    left untouched."""
    pts = [(float(p[0]), float(p[1])) for p in points]
    if len(pts) < 2 or width_mm <= 0.0:
        return pts
    if tol_mm is None:
        tol_mm = max(1e-4 * diameter_mm, 1e-6)
    ext = width_mm * 1.5 + 1.0

    def push(end, prev):
        ex, ey = end
        dist, nx, ny = _nearest_hex_edge(ex, ey, diameter_mm)
        if dist > tol_mm:
            return end
        dx, dy = ex - prev[0], ey - prev[1]
        m = math.hypot(dx, dy)
        if m < 1e-9:
            return end
        dx, dy = dx / m, dy / m
        if dx * nx + dy * ny <= 1e-3:   # heading inward / along the rim
            return end
        return (ex + dx * ext, ey + dy * ext)

    pts[-1] = push(pts[-1], pts[-2])
    pts[0] = push(pts[0], pts[1])
    return pts


def path_corridor_polygons(points, width_mm, diameter_mm):
    """Turn an open polyline (tile-local mm) into the paved area of a
    Flagstone Path Feature: the line buffered by `width_mm/2` on each side
    (miter joins, flat end caps), clipped to the tile hexagon so no stone
    ever crosses the rim. Returns a list of CCW polygons — normally exactly
    one; if a bend is too tight for a single clean outline (miter beyond
    `_CORRIDOR_MITER_LIMIT`, or the outline self-intersects) it falls back
    to one quad per polyline segment. Each polygon is fed straight into
    `scatter_flagstones`, the same tessellator the FLAGSTONE Draw Area
    surface uses."""
    pts = []
    for p in points:
        p = (float(p[0]), float(p[1]))
        if not pts or math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) > 1e-9:
            pts.append(p)
    half = width_mm * 0.5
    if len(pts) < 2 or half <= 0.0:
        return []
    # A rim-snapped end is extended past the rim so the hex clip below
    # squares it off exactly along the hex edge.
    pts = extend_rim_endpoints(pts, width_mm, diameter_mm)

    def seg_normal(a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        m = math.hypot(dx, dy)
        return (-dy / m, dx / m)

    normals = [seg_normal(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]

    def quad(a, b, n):
        return [(a[0] - n[0] * half, a[1] - n[1] * half),
                (b[0] - n[0] * half, b[1] - n[1] * half),
                (b[0] + n[0] * half, b[1] + n[1] * half),
                (a[0] + n[0] * half, a[1] + n[1] * half)]

    left, right = [], []
    too_tight = False
    for i, p in enumerate(pts):
        if i == 0:
            n, scale = normals[0], 1.0
        elif i == len(pts) - 1:
            n, scale = normals[-1], 1.0
        else:
            n0, n1 = normals[i - 1], normals[i]
            mx, my = n0[0] + n1[0], n0[1] + n1[1]
            mm = math.hypot(mx, my)
            if mm < 1e-9:
                too_tight = True
                break
            n = (mx / mm, my / mm)
            cos_half = n[0] * n0[0] + n[1] * n0[1]
            scale = 1.0 / cos_half if cos_half > 1e-9 else float("inf")
            if scale > _CORRIDOR_MITER_LIMIT:
                too_tight = True
                break
        left.append((p[0] + n[0] * half * scale, p[1] + n[1] * half * scale))
        right.append((p[0] - n[0] * half * scale, p[1] - n[1] * half * scale))

    if not too_tight:
        outline = right + left[::-1]
        if _polygon_is_simple(outline):
            raw = [outline]
        else:
            too_tight = True
    if too_tight:
        raw = [quad(pts[i], pts[i + 1], normals[i]) for i in range(len(pts) - 1)]

    out = []
    for poly in raw:
        if _signed_area(poly) < 0.0:
            poly = poly[::-1]
        clipped = _clip_to_hex(poly, diameter_mm)
        if len(clipped) >= 3 and abs(_signed_area(clipped)) > 1e-6:
            if _signed_area(clipped) < 0.0:
                clipped = clipped[::-1]
            out.append(clipped)
    return out


def scatter_flagstones(polygon, *, min_size_mm, max_size_mm, gap_mm, seed):
    """Plan flagstone placements inside `polygon` (tile-local mm) as a power
    diagram (weighted Voronoi) tessellation, each cell inset for mortar.
    Returns a list of `(x_mm, y_mm, outline_local, pid)` tuples — `(x, y)` a
    seed centre, `outline_local` its own inset cell's vertices (CCW) relative
    to that centre, Z/tilt resolved later by the caller's per-corner
    sampling. `min/max_size_mm` are DIAMETERS, used as each seed's power-
    diagram weight so a bigger drawn size actually claims more area (not a
    literal per-stone bound the way a packer's direct draw was — a cell's
    final size also depends on its neighbours', same as any tessellation).
    Deterministic from `seed` so rebuilds are stable.

    Algorithm: seed the region with a hex-staggered grid (pitch = the mean of
    min/max size) plus the centroid, each jittered in position and given its
    own random target size/weight — the same reseed-grid construction the
    earlier packer used only as a fallback, now the sole placement
    mechanism, since a full tessellation needs no "growth front" at all.
    Each seed's cell is `_power_cell` (clipped to `polygon`, normalised to
    CCW winding first so the inset step's outward-normal math is valid
    regardless of how the caller wound it) then `_inset_cell` (the mortar
    gap). A tessellation covers the whole region by construction, so this
    produces near-complete coverage with no growth-front holes — the
    previous algorithm's actual failure mode, which read as "much wider
    gaps than the parameter" even though its pairwise minimum-gap invariant
    held.

    `_power_cell`'s cells are guaranteed convex (so `_inset_cell`'s own-edge
    offset is exact) whenever `polygon` itself is convex — true for the
    default whole-hex case. A hand-drawn, genuinely concave Draw Area region
    can in principle leave the one or two cells that straddle its concave
    part non-convex, where the inset is only an approximation (still safe —
    `_intersect_halfplanes` never corrupts, at worst insets slightly
    unevenly right at that notch); not a concern for the common case."""
    if len(polygon) < 3:
        return []
    lo = max(0.0, min(min_size_mm, max_size_mm))
    hi = max(0.0, max(min_size_mm, max_size_mm))
    if hi <= 0.0:
        return []
    gap_mm = max(0.0, gap_mm)
    seed = int(seed)

    boundary = list(polygon)
    if _signed_area(boundary) < 0.0:
        boundary = list(reversed(boundary))

    mean_size = 0.5 * (lo + hi)
    pitch = max(mean_size, 1e-6)

    xs = [p[0] for p in boundary]
    ys = [p[1] for p in boundary]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    bbox_w = max(maxx - minx, 1e-6)
    bbox_h = max(maxy - miny, 1e-6)

    est_cells = (bbox_w / pitch + 2) * (bbox_h / pitch + 2)
    if est_cells > _FLAGSTONE_MAX_SEEDS:
        pitch *= math.sqrt(est_cells / _FLAGSTONE_MAX_SEEDS)

    seeds = []   # [(x, y, weight), ...]

    def add_seed(x, y, salt_x, salt_y):
        if len(seeds) >= _FLAGSTONE_MAX_SEEDS:
            return
        u = _hash01(salt_x, salt_y, seed ^ 0x1234ABCD)
        size = lo + (hi - lo) * u
        seeds.append((x, y, (0.5 * size) ** 2))

    centroid = (sum(xs) / len(xs), sum(ys) / len(ys))
    if point_in_polygon(centroid[0], centroid[1], boundary):
        add_seed(centroid[0], centroid[1], 0, 0)

    nrows = int(math.ceil(bbox_h / pitch)) + 2
    ncols = int(math.ceil(bbox_w / pitch)) + 2
    for row in range(nrows):
        if len(seeds) >= _FLAGSTONE_MAX_SEEDS:
            break
        yv = miny + row * pitch
        offset = 0.5 * pitch if row % 2 else 0.0
        for col in range(ncols):
            if len(seeds) >= _FLAGSTONE_MAX_SEEDS:
                break
            xv = minx + col * pitch + offset
            jx = (_hash01(col, row, seed) - 0.5) * pitch * 0.6
            jy = (_hash01(col, row, seed ^ 0x9E3779B9) - 0.5) * pitch * 0.6
            px, py = xv + jx, yv + jy
            if not point_in_polygon(px, py, boundary):
                continue
            add_seed(px, py, col + 1, row + 1)

    if not seeds:
        return []

    # Spatial hash for the power-cell neighbour search. Only a bucketing
    # convenience now, not a correctness-critical reach bound: `_power_cell`
    # widens its own search radius as needed, since a fixed one isn't safe
    # (see its docstring).
    bucket_size = max(pitch, 1e-6)
    grid = {}
    for idx, (sx, sy, _w) in enumerate(seeds):
        key = (int(math.floor(sx / bucket_size)), int(math.floor(sy / bucket_size)))
        grid.setdefault(key, []).append(idx)

    # Largest possible |weight_i - weight_j| over the whole min/max_size_mm
    # range -- the conservative bound `_power_cell` needs to know how far a
    # not-yet-considered seed's bisector could still reach back toward `i`.
    weight_span = max(0.0, (0.5 * hi) ** 2 - (0.5 * lo) ** 2)

    # Threshold for merging away a near-duplicate vertex left behind by
    # sequential half-plane clipping (see `_dedupe_polygon_vertices`) -- tied
    # to the configured stone scale rather than a fixed mm constant, so it
    # stays a negligible fraction of a stone's own size at any model scale.
    min_edge_mm = max(1e-9, 0.005 * mean_size)

    placements = []
    for i, (sx, sy, sw) in enumerate(seeds):
        cell_verts, cell_tags = _power_cell(i, seeds, boundary, grid,
                                            bucket_size, weight_span, min_edge_mm)
        if len(cell_verts) < 3:
            continue
        inset_verts = _inset_cell(cell_verts, cell_tags, gap_mm, i,
                                  seed ^ 0x5A17E000)
        inset_verts, _inset_tags = _dedupe_polygon_vertices(
            inset_verts, [None] * len(inset_verts), min_edge_mm)
        if len(inset_verts) < 3:
            continue
        # A seed right next to a tight/acute boundary corner (a hand-drawn
        # or flood-filled region's own boundary -- the hex's own 6 corners
        # are never this acute) can get clipped into a sliver far smaller
        # than its own drawn size -- geometrically a valid tessellation
        # cell, but not a usable paving stone: too small to print/see as
        # its own piece, and (being so small) disproportionately sensitive
        # to any local height variation, so it tilts hard against whatever
        # the terrain happens to do right at that one corner -- visually a
        # single oddly-angled sliver among otherwise normal stones. Skip it
        # (leaving a small bare patch there) rather than place it -- `sw`
        # (seed `i`'s own weight, `(size/2)**2`) times pi is exactly that
        # seed's own nominal circular area, so this is relative to what the
        # seed itself was aiming for, not a fixed mm^2 constant.
        if polygon_area(inset_verts) < _FLAGSTONE_MIN_CELL_AREA_FRACTION * math.pi * sw:
            continue
        outline_local = [(x - sx, y - sy) for (x, y) in inset_verts]
        pid = ((i * 2654435761) ^ seed) & 0x7FFFFFFF
        placements.append((sx, sy, outline_local, pid))
    return placements


def estimate_flagstone_count(polygon, *, min_size_mm, max_size_mm, gap_mm):
    """Rough expected flagstone count for a scatter region — cheap enough for
    a live panel warning. Mirrors estimate_boulder_count's shape; the real
    tessellation covers the region near-completely (unlike a packer hitting
    a target density), so this is simply area / (mean cell area) with a
    small fudge for the mortar-joint area it doesn't otherwise account for.
    `gap_mm` no longer inflates the pitch (the gap comes from insetting
    cells, not from spacing them further apart)."""
    area = polygon_area(polygon)
    lo = max(0.0, min(min_size_mm, max_size_mm))
    hi = max(0.0, max(min_size_mm, max_size_mm))
    mean_size = 0.5 * (lo + hi)
    if area <= 0.0 or mean_size <= 0.0:
        return 0
    pitch = max(mean_size, 1e-6)
    return int(area / (pitch * pitch) * _FLAGSTONE_COVERAGE_FACTOR)


# ---------------------------------------------------------------------------
# Polyline -> corridor polygon — turns a Path Feature's drawn line + width
# into a region polygon `scatter_flagstones` can tessellate, so a flagstone
# path reuses the exact same placement/mesh/tilt-fit code as a flagstone
# Draw Area region. Path Feature's existing `width_mm` has never needed an
# explicit polygon before — everywhere else (tree_pads.py) it's just a
# per-vertex radial distance test against the centerline — so this is new.
# ---------------------------------------------------------------------------
def polyline_corridor_polygon(points, width_mm, *, miter_limit=4.0):
    """Buffer open polyline `points` (`[(x, y), ...]`, >= 2 points after
    dropping duplicate consecutive ones) into a closed `width_mm`-wide
    ribbon polygon centred on it — standard stroke-to-fill polyline
    buffering. Returns `[]` for fewer than 2 distinct points.

    At each interior point, the offset direction is the normalised average
    of its two adjacent segments' left-of-travel perpendiculars (a
    standard mitred join) scaled by `1 / cos(half the turn angle)` so the
    two segments' offset edges actually meet there — clamped to
    `miter_limit` x the half-width to avoid an unbounded spike on a sharp
    turn (falls back to the unmitred per-segment offset beyond that, a
    plain bevel instead of a spike — the same "safety clamp against one
    extreme case" idiom `clamp_tilt_slopes` uses elsewhere in this
    codebase). The two endpoints use their one adjacent segment's own
    (unmitred) perpendicular directly — no cap there to begin with.

    Winding is whatever falls out of the construction — not normalised
    here, since every caller (`scatter_flagstones` first among them) already
    normalises its own `polygon` argument via `_signed_area` regardless of
    where it came from.

    Not self-intersection-safe for a very sharp turn on a very short
    segment (the inner mitred corner can fall past the far end of the
    short segment) — acceptable for a hand-drawn path with reasonably
    spaced waypoints, the same kind of accepted limitation this codebase
    already lives with for a genuinely concave Draw Area region (see
    `scatter_flagstones`'s own docstring)."""
    pts = []
    for (px, py) in points:
        if not pts or math.hypot(px - pts[-1][0], py - pts[-1][1]) > 1e-9:
            pts.append((float(px), float(py)))
    n = len(pts)
    if n < 2:
        return []
    half = max(width_mm, 0.0) * 0.5

    def seg_dir(i):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length < 1e-9:
            return (1.0, 0.0)
        return (dx / length, dy / length)

    seg_perps = []
    for i in range(n - 1):
        dx, dy = seg_dir(i)
        seg_perps.append((-dy, dx))   # left-of-travel unit perpendicular

    offsets = []
    for i in range(n):
        if i == 0:
            offsets.append(seg_perps[0])
        elif i == n - 1:
            offsets.append(seg_perps[-1])
        else:
            px, py = seg_perps[i - 1]
            qx, qy = seg_perps[i]
            bx, by = px + qx, py + qy
            blen = math.hypot(bx, by)
            if blen < 1e-9:
                # A ~180-degree reversal has no well-defined miter -- fall
                # back to the incoming segment's own unmitred offset.
                offsets.append((px, py))
                continue
            bx, by = bx / blen, by / blen
            cos_half = bx * px + by * py   # always in [0, 1] by construction
            scale = 1.0 / max(cos_half, 1.0 / miter_limit)
            offsets.append((bx * scale, by * scale))

    left = [(x + nx * half, y + ny * half)
           for (x, y), (nx, ny) in zip(pts, offsets)]
    right = [(x - nx * half, y - ny * half)
            for (x, y), (nx, ny) in zip(pts, offsets)]
    return left + list(reversed(right))


def clip_polygon_to_hex(polygon, diameter_mm):
    """Clip `polygon` (any winding, possibly concave — e.g. a
    `polyline_corridor_polygon` result) to the hex's own footprint
    (`hex_polygon(diameter_mm)`) via `_intersect_halfplanes`. A path's
    buffered corridor can extend past the hex's physical rim right where
    the path is deliberately continuing onto a neighbouring tile — unlike a
    hand-drawn or Flood-Filled region, which is already implicitly bounded
    by the mesh itself — so it must be clipped before tessellating. Returns
    `[]` if nothing of `polygon` survives."""
    hexp = hex_polygon(diameter_mm)
    if _signed_area(hexp) < 0.0:
        hexp = list(reversed(hexp))
    halfplanes = []
    n = len(hexp)
    for k in range(n):
        ax, ay = hexp[k]
        bx, by = hexp[(k + 1) % n]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        nx, ny = dy / length, -dx / length
        c = nx * ax + ny * ay
        halfplanes.append((nx, ny, c, None))
    verts, _tags = _intersect_halfplanes(polygon, halfplanes)
    return verts


def _rotate_to_normal(nx, ny, nz):
    """3x3 rotation matrix (3 row-tuples) mapping (0,0,1) to the unit vector
    (nx, ny, nz) via the minimal (shortest-arc) rotation — Rodrigues' formula
    on the axis cross((0,0,1),(nx,ny,nz)). Pure-Python analogue of
    `mathutils.Vector.rotation_difference`, used the same way by
    segment_path.py for a placed segment's tilt; needed here in plain
    vector math because this module stays bpy-free. Returns the identity
    matrix when the normal is already ~+Z, or a 180-degree flip about X when
    it's ~-Z (an edge case `clamp_tilt_slopes` makes unreachable in practice,
    handled defensively anyway)."""
    ax, ay = -ny, nx   # cross((0, 0, 1), (nx, ny, nz)) = (-ny, nx, 0)
    sin_t = math.hypot(ax, ay)
    cos_t = nz
    if sin_t < 1e-12:
        if cos_t >= 0.0:
            return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        return ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
    kx, ky = ax / sin_t, ay / sin_t
    one_c = 1.0 - cos_t
    return (
        (cos_t + kx * kx * one_c, kx * ky * one_c, ky * sin_t),
        (kx * ky * one_c, cos_t + ky * ky * one_c, -kx * sin_t),
        (-ky * sin_t, kx * sin_t, cos_t),
    )


def _apply_rotation(mat, v):
    r0, r1, r2 = mat
    x, y, z = v
    return (r0[0] * x + r0[1] * y + r0[2] * z,
            r1[0] * x + r1[1] * y + r1[2] * z,
            r2[0] * x + r2[1] * y + r2[2] * z)


# A flagstone's outline corner is routinely exactly on the region polygon's
# own boundary (mortar insets never apply to the paved area's rim), which
# can coincide with the hex's physical rim -- the one place a raycast-based
# `corner_sampler` is most prone to a wrong reading (see flagstones.py's
# `_make_corner_sampler`: a miss, or a "fall through" hit on the side wall
# or bottom plate). Rather than relying solely on the bpy-side sampler to
# recover from that, `assemble_flagstone_mesh` sidesteps it here too: it
# never actually samples EXACTLY at a corner, only a point nudged this
# fraction of the way from the corner back toward the stone's own centroid
# (always known here, always strictly inside the stone's own footprint) --
# tiny enough (a sub-millimetre shift even for a large stone) to make no
# perceptible difference to the height actually read, but enough to clear
# any exact-boundary degeneracy before the sample is even taken.
_SAMPLE_INWARD_SHRINK = 0.002


def _shrink_toward_centroid(cx, cy, x, y):
    return (cx + (x - cx) * (1.0 - _SAMPLE_INWARD_SHRINK),
            cy + (y - cy) * (1.0 - _SAMPLE_INWARD_SHRINK))


# Fractions (centroid -> corner / edge midpoint) at which the bottom face's
# interior is sampled for the no-penetration lift, on top of the exact
# corners and edge midpoints themselves.
_INTERIOR_SAMPLE_RINGS = (0.25, 0.5, 0.75)


def _bottom_face_samples(bottom_ring):
    """`[(x, y, z_bottom), ...]` points ON a stone's (planar, convex) bottom
    face: the exact corners, every edge midpoint, the vertex centroid, and
    each corner/midpoint pulled toward that centroid at every
    `_INTERIOR_SAMPLE_RINGS` fraction. Since the ring is planar, linear
    interpolation between the centroid and any boundary point stays exactly
    on the face, so `z_bottom` needs no plane evaluation."""
    n = len(bottom_ring)
    cx = sum(p[0] for p in bottom_ring) / n
    cy = sum(p[1] for p in bottom_ring) / n
    cz = sum(p[2] for p in bottom_ring) / n
    boundary = list(bottom_ring)
    for i in range(n):
        a, b = bottom_ring[i], bottom_ring[(i + 1) % n]
        boundary.append(((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5,
                         (a[2] + b[2]) * 0.5))
    out = list(boundary)
    out.append((cx, cy, cz))
    for t in _INTERIOR_SAMPLE_RINGS:
        for (bx, by, bz) in boundary:
            out.append((cx + (bx - cx) * t, cy + (by - cy) * t,
                        cz + (bz - cz) * t))
    return out


def assemble_flagstone_mesh(placements, corner_sampler, *, thickness_mm=60.0,
                            sink_mm=1.0, max_tilt_deg=25.0):
    """Join every flagstone in `placements` into ONE `(verts, faces)` mesh,
    each individually TILTED to rest flush on the local terrain rather than
    just raycast-seated flat at its centre like a boulder.

    For each placement: build the stone via `flagstone_mesh` from its own
    `outline_local` (no separate spin — the outline is already a unique,
    oriented tessellation cell), sample the real surface Z under every one
    of its BOTTOM-ring corners — the contact face, z=0 in the stone's own
    local frame — via the injected `corner_sampler(x, y)` callback (keeping
    this bpy-free/testable, mirroring boulders' injected `z_of`) — nudged
    very slightly toward the stone's own centroid first
    (`_shrink_toward_centroid`/`_SAMPLE_INWARD_SHRINK`), so a corner sitting
    exactly on the region's own boundary is never the literal point the FIT
    samples (this is a deliberate, bounded bias — tiny enough to be
    inconsequential to the fitted plane, see `test_assembly_tilts_to_match_a_sloped_plane`'s
    own tolerance — traded for never fitting against a single at-risk
    exact-boundary sample) — fit the lowest never-penetrating plane through
    those samples (`segment_geometry.fit_resting_plane` — the same "rests on
    N uneven point supports without sinking through" fit `segment_path.py`
    uses for a placed path segment — falling back to its least-squares
    `fit_tilt_plane` only on the rare fully-degenerate input the resting fit
    can't handle), clamp the tilt (`segment_geometry.clamp_tilt_slopes`),
    turn the plane's normal into a rigid rotation about the fitted pivot and
    rebuild each world vertex from it, then do one more corner check:
    `fit_resting_plane` only guarantees "no sinking" against its own LINEAR
    plane model evaluated at each corner's ORIGINAL (pre-rotation) position —
    on genuinely non-planar terrain, a rotated corner's real final (x, y)
    drifts slightly off that flat position (a true rotation, unlike a linear
    plane, doesn't preserve X/Y), so the actual terrain sampled there can
    differ from what the fit saw. Re-sampling `corner_sampler` at each
    corner's real final (x, y) — the EXACT position this time, deliberately
    NOT nudged, since this is the safety net that must guarantee no sinking
    against the true terrain, not a lerped stand-in for it — and lifting the
    whole rigid stone straight up by whatever small amount clears the worst
    case restores the "never penetrates" guarantee at the true terrain
    rather than just the flat linear model of it — the same corner re-check
    `segment_path.py`'s own placement fit performs for a placed path
    segment, for the identical reason. That recheck also covers the bottom
    face's INTERIOR (`_bottom_face_samples`: edge midpoints, centroid and
    rings in between), since the fit itself only sees the corners and
    convex/bumpy terrain between them would otherwise poke up through the
    slab. Finally applies the `sink_mm` overlap
    (so a later boolean merge stays manifold, same as boulders). The stones
    share no vertices, so the concatenation of closed 2-manifold prisms is
    itself 2-manifold."""
    all_verts = []
    all_faces = []
    for (x, y, outline_local, _pid) in placements:
        n = len(outline_local)
        if n < 3:
            continue
        lverts, lfaces = flagstone_mesh(outline_local, thickness_mm)

        samples = [(lx, ly, corner_sampler(*_shrink_toward_centroid(
                       x, y, x + lx, y + ly)))
                  for (lx, ly) in outline_local]

        plane = sg.fit_resting_plane(samples)
        if plane is None:
            plane = sg.fit_tilt_plane(samples)
        if plane is None:
            px, py, pz = 0.0, 0.0, corner_sampler(x, y)
            slope_x, slope_y = 0.0, 0.0
        else:
            px, py, pz, slope_x, slope_y = plane
            slope_x, slope_y = sg.clamp_tilt_slopes(slope_x, slope_y, max_tilt_deg)

        normal = _normalize((-slope_x, -slope_y, 1.0))
        rot_mat = _rotate_to_normal(*normal)

        world = []
        for (vx, vy, vz) in lverts:
            rvx, rvy, rvz = _apply_rotation(rot_mat, (vx - px, vy - py, vz))
            world.append((x + px + rvx, y + py + rvy, pz + rvz))

        # Unlike the fit samples above, this safety-net recheck deliberately
        # samples each corner's own TRUE final position, not a nudged one --
        # it's the thing that guarantees the stone never sinks below the real
        # terrain, so it must read the real terrain exactly there. It also
        # samples the bottom face's INTERIOR (`_bottom_face_samples`): the
        # fit only ever saw the corners, so on a convex/bumpy surface (a
        # hill, a Surface Texture) the terrain between the corners would
        # otherwise poke straight up through the thin slab -- the reported
        # "flagstones sink into the surface" bug.
        lift = 0.0
        for (wx, wy, wz) in _bottom_face_samples(world[:n]):
            gap = wz - corner_sampler(wx, wy)
            if gap < lift:
                lift = gap
        shift = -lift - sink_mm   # lift <= 0, so -lift >= 0 clears the worst sample

        base = len(all_verts)
        for (wx, wy, wz) in world:
            all_verts.append((wx, wy, wz + shift))
        for f in lfaces:
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
        Surface("FLAGSTONE", "Flagstone Paving",
                "Large irregular paving slabs, individually tilted to rest "
                "flush on the terrain (does not change the tile surface)",
                reference_mm=0.0, generator=None,
                kind='scatter', placement_fn=scatter_flagstones,
                element_mesh_fn=flagstone_mesh, uses_feature=False,
                extra_params=(
                    ParamSpec("min_size_mm", "Min Stone Size (mm)",
                              "Diameter of the smallest flagstones",
                              420.0, 50.0, 2000.0, unit='mm',
                              reference_mm=420.0),
                    ParamSpec("max_size_mm", "Max Stone Size (mm)",
                              "Diameter of the largest flagstones",
                              650.0, 50.0, 2000.0, unit='mm',
                              reference_mm=650.0),
                    ParamSpec("gap_mm", "Avg. Mortar Gap (mm)",
                              "Average gap maintained between neighbouring "
                              "flagstones -- actual gaps vary, and stones "
                              "may touch in places",
                              10.0, 0.0, 100.0, unit='mm', reference_mm=10.0),
                    ParamSpec("thickness_mm", "Slab Thickness (mm)",
                              "Thickness of each flagstone slab",
                              60.0, 0.2, 300.0, unit='mm', reference_mm=60.0),
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
