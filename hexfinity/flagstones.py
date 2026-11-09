"""bpy shell for the *Flagstone Paving* scatter surface — sibling to
`scatter.py` (Boulder Field).

Mirrors scatter.py's structure (object lifecycle, raycast seating, boolean
merge) but with its own tags and its own per-CORNER raycast sampler, since a
flagstone needs a multi-point tilt fit (`procedural_surfaces.assemble_flagstone_mesh`,
via `segment_geometry.fit_resting_plane`) rather than boulders' single
centre-height seating — different enough in shape that, per this codebase's
established convention (small independent copies over a shared abstraction
for similar-but-not-identical mechanisms), this is a separate module rather
than a generalized scatter.py.

`sync_flagstones` runs from `operators.rebuild_tile`, inside the
`_REBUILDING` guard, so the object churn it does never recurses back into a
rebuild.
"""

import bpy
from mathutils import Vector

from . import procedural_surfaces as ps
from .manifold_check import assert_two_manifold


# Tag keys written onto each generated flagstone object.
FLAGSTONE_OF = "hf_flagstone_of"          # region key this object belongs to
FLAGSTONE_MERGE = "hf_flagstone_merge"    # per-region "merge into tile" pref

# How far each stone sinks into the terrain so it overlaps the surface —
# tangent stones make a boolean merge non-manifold; a small overlap is clean.
# `man_height_mm` defaults to 10 (HexFinityMapProperties.man_height_mm) and is
# routinely set well below life-size (28mm wargaming scale and smaller), which
# scales `thickness_mm` down with it — a FIXED sink would then exceed (or
# swallow) a thin slab's own thickness, burying the whole stone. Scale the
# sink with thickness instead, capped at the old fixed value so full-scale
# behaviour (thickness_mm in the tens of mm) is unchanged.
SINK_FRACTION = 0.15
SINK_MM_MAX = 1.0
# Tilt safety clamp. Kept as a module constant (not a UI param) the same way
# scatter.py hardcodes ROUGHNESS/SUBDIV — the 4 user-facing slots go to
# size/gap/thickness instead.
MAX_TILT_DEG = 25.0

# Each stone's outline is now its own tessellation cell (see
# `procedural_surfaces.scatter_flagstones`), not a fixed-`sides` n-gon, so
# there is no true per-element vertex count any more. This is only a rough
# average used by panel.py's vertex-budget warning estimate.
AVG_OUTLINE_VERTS_ESTIMATE = 6


def region_key(region_idx):
    """Stable tag identifying the flagstone objects of region `region_idx`."""
    return f"region_{region_idx}"


# ---------------------------------------------------------------------------
# Object lifecycle
# ---------------------------------------------------------------------------
def _remove_object(obj):
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def purge_flagstones(tile_obj, key=None):
    """Remove the generated flagstone child object(s) of `tile_obj`. With
    `key`, only that region's object; otherwise every `hf_flagstone_*` child.
    Safe to call when there is nothing to remove."""
    for child in list(tile_obj.children):
        tag = child.get(FLAGSTONE_OF)
        if tag is None:
            continue
        if key is None or tag == key:
            _remove_object(child)


def _unique_object_name(base):
    """`base`, or `base_00`, `base_01`, … if taken — so two regions that share
    an area name don't collide (Blender's default `.001` suffix is less
    legible)."""
    if base not in bpy.data.objects:
        return base
    i = 0
    while f"{base}_{i:02d}" in bpy.data.objects:
        i += 1
    return f"{base}_{i:02d}"


def _make_corner_sampler(tile_obj, depsgraph, fallback_z):
    """Return `corner_sampler(x, y)` → tile-LOCAL surface Z (mm) under the
    local point, found by raycasting straight down onto the tile's evaluated
    mesh (BVH-accelerated — fast regardless of how many top faces the tile
    has, unlike a linear scan over every triangle). Falls back to
    `fallback_z` for the rare miss (a point grazing the rim) — same shape as
    scatter.py's `_make_z_of`, just called once per flagstone CORNER instead
    of once per element, since each stone needs a multi-point tilt fit rather
    than a single seating height.

    `fallback_z` must be returned on every miss, not whatever the previous
    call happened to hit: an earlier revision used a closure-captured
    mutable "last successful hit" as the miss fallback, which silently
    carried a stale, unrelated height across every corner of every stone in
    the region once any single raycast missed (most likely a corner sitting
    exactly on the region's own boundary, which can coincide with the hex's
    physical rim — the knife-edge between the top face and the near-vertical
    side wall where a straight-down raycast can numerically miss) — a real,
    reported bug producing tilted/floating stones. A barycentric-
    interpolation replacement (no raycast, no miss case at all) was tried
    next, but its unaccelerated per-call scan over every top triangle made a
    Flood-Filled (often thousands-of-triangles) region's full placement pass
    pathologically slow -- effectively locking up Blender. Back to the
    raycast, with just the fallback value fixed."""
    down = Vector((0.0, 0.0, -1.0))

    def corner_sampler(x, y):
        hit, loc, _n, _i = tile_obj.ray_cast(
            Vector((x, y, 1.0e5)), down, depsgraph=depsgraph)
        return loc.z if hit else fallback_z

    return corner_sampler


def sync_flagstones(tile_obj, region_idx, region_dict, name):
    """(Re)build the joined flagstone object for one scatter region.

    `region_dict` is the marshalled region (see `operators.rebuild_tile`) with
    at least `polygon`, `extras` (min/max size, gap, thickness) and `seed`.
    The object is named `Flagstones_<name>`, parented under `tile_obj`, linked
    into the tile's collection(s) and tagged so a later rebuild/purge can find
    it.

    Assumes the caller already purged any prior object for this region and ran
    inside `operators._REBUILDING`.
    """
    extras = region_dict.get("extras", {})
    polygon = region_dict.get("polygon") or ps.hex_polygon(
        bpy.context.scene.hexfinity_map.diameter_mm)

    placements = ps.scatter_flagstones(
        polygon,
        min_size_mm=extras.get("min_size_mm", 420.0),
        max_size_mm=extras.get("max_size_mm", 650.0),
        gap_mm=extras.get("gap_mm", 10.0),
        seed=region_dict.get("seed", 0),
    )
    if not placements:
        return None

    # Seat/tilt the flagstones onto the real terrain. base_thickness is the
    # lowest the surface can be, so it's a safe fallback for a raycast miss.
    depsgraph = bpy.context.evaluated_depsgraph_get()
    fallback_z = bpy.context.scene.hexfinity_map.base_thickness_mm
    corner_sampler = _make_corner_sampler(tile_obj, depsgraph, fallback_z)

    thickness_mm = extras.get("thickness_mm", 60.0)
    sink_mm = min(SINK_MM_MAX, thickness_mm * SINK_FRACTION)
    verts, faces = ps.assemble_flagstone_mesh(
        placements, corner_sampler,
        thickness_mm=thickness_mm, sink_mm=sink_mm, max_tilt_deg=MAX_TILT_DEG)
    assert_two_manifold(verts, faces)

    obj_name = _unique_object_name(f"Flagstones_{name}")
    mesh = bpy.data.meshes.new(obj_name)
    mesh.from_pydata(verts, [], faces)
    mesh.update(calc_edges=True)

    obj = bpy.data.objects.new(obj_name, mesh)
    for coll in tile_obj.users_collection:
        coll.objects.link(obj)
    obj.parent = tile_obj            # default matrix_parent_inverse (identity):
    obj.location = (0.0, 0.0, 0.0)   # flagstone verts are already tile-local mm
    obj[FLAGSTONE_OF] = region_key(region_idx)
    obj[FLAGSTONE_MERGE] = bool(region_dict.get("scatter_merge", False))
    return obj


# ---------------------------------------------------------------------------
# Printability — boolean-union the loose flagstones into the tile.
# ---------------------------------------------------------------------------
class HEXFINITY_OT_merge_flagstones(bpy.types.Operator):
    bl_idname = "hexfinity.merge_flagstones"
    bl_label = "Merge Flagstones into Tile"
    bl_description = ("Boolean-union this tile's flagstone objects into the "
                      "tile mesh so the whole tile prints as one manifold "
                      "piece. Regenerating the tile recreates the loose "
                      "flagstones.")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None and obj.hexfinity_tile.is_generated
                and any(c.get(FLAGSTONE_OF) is not None for c in obj.children))

    def execute(self, context):
        tile_obj = context.active_object
        candidates = [c for c in tile_obj.children
                      if c.get(FLAGSTONE_OF) is not None]
        # Respect per-region "Merge into Tile" if any are flagged; otherwise an
        # explicit button press merges everything.
        flagged = [c for c in candidates if c.get(FLAGSTONE_MERGE)]
        to_merge = flagged or candidates
        if not to_merge:
            self.report({'WARNING'}, "No flagstones to merge")
            return {'CANCELLED'}

        for src in to_merge:
            mod = tile_obj.modifiers.new(name="hf_flagstone_merge", type='BOOLEAN')
            mod.operation = 'UNION'
            mod.solver = 'EXACT'
            mod.object = src
        with context.temp_override(active_object=tile_obj,
                                   selected_objects=[tile_obj],
                                   object=tile_obj):
            for mod in [m for m in tile_obj.modifiers
                        if m.name.startswith("hf_flagstone_merge")]:
                bpy.ops.object.modifier_apply(modifier=mod.name)

        for src in to_merge:
            _remove_object(src)

        # Verify the merge stayed manifold (loud failure beats a silent bad STL).
        mesh = tile_obj.data
        verts = [tuple(v.co) for v in mesh.vertices]
        faces = [tuple(p.vertices) for p in mesh.polygons]
        try:
            assert_two_manifold(verts, faces)
        except Exception as exc:
            self.report({'WARNING'}, f"Merged, but mesh is not 2-manifold: {exc}")
            return {'FINISHED'}

        self.report({'INFO'}, f"Merged {len(to_merge)} flagstone object(s) into tile")
        return {'FINISHED'}
