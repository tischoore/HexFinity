"""bpy shell for *scatter* surfaces (Boulder Field).

Mirrors `regions.py`'s role for the displacement surfaces: all the geometry math
lives bpy-free in `procedural_surfaces` (placement planner, boulder mesh,
assembler) — this module is the thin Blender layer that the pure code can't do:

  * a Z-raycast that seats each boulder on the REAL terrain (after Coons /
    displacement / brush / height edits — robust regardless of how the height
    was produced),
  * object lifecycle (create / replace / parent / tag the joined mesh), and
  * an optional boolean *merge* so the whole tile prints as one manifold piece.

`sync_scatter` runs from `operators.rebuild_tile`, inside the `_REBUILDING`
guard, so the object churn it does never recurses back into a rebuild.
"""

import math

import bpy
from mathutils import Vector

from . import procedural_surfaces as ps
from .manifold_check import assert_two_manifold

# A raycast exactly on the region polygon's own boundary -- which, for a
# Flood Fill selection, routinely coincides with an actual mesh rim vertex
# (the boundary is walked directly off the mesh's own verts) -- can miss:
# the ray is coplanar with/parallel to the near-vertical side wall there,
# or lands exactly on a vertex several faces share, both classic degenerate
# cases for a ray-triangle intersection test. `_MISS_RETRY_RADII_MM` nudges
# the query point in a small ring of directions at escalating radii until
# one lands cleanly inside a face, rather than risking a wrong fallback
# height for that one point (see `_make_z_of`).
_MISS_RETRY_RADII_MM = (0.05, 0.5, 5.0)
_MISS_RETRY_DIRECTIONS = 8


# Tag keys written onto each generated boulder object.
SCATTER_OF = "hf_scatter_of"          # region key this object belongs to
SCATTER_MERGE = "hf_scatter_merge"    # per-region "merge into tile" preference

# How far each boulder sinks into the terrain (mm) so it overlaps the surface —
# tangent boulders make a boolean merge non-manifold; a small overlap is clean.
SINK_MM = 1.0
# Boulder mesh detail. Kept modest so a dense field stays a sane vertex count.
ROUGHNESS = 0.4
SUBDIV = 1


def region_key(region_idx):
    """Stable tag identifying the scatter objects of region `region_idx`."""
    return f"region_{region_idx}"


# ---------------------------------------------------------------------------
# Object lifecycle
# ---------------------------------------------------------------------------
def _remove_object(obj):
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def purge_scatter(tile_obj, key=None):
    """Remove the generated boulder child object(s) of `tile_obj`. With `key`,
    only that region's object; otherwise every `hf_scatter_*` child. Safe to call
    when there is nothing to remove."""
    for child in list(tile_obj.children):
        tag = child.get(SCATTER_OF)
        if tag is None:
            continue
        if key is None or tag == key:
            _remove_object(child)


def _unique_object_name(base):
    """`base`, or `base_00`, `base_01`, … if taken — so two regions that share an
    area name don't collide (Blender's default `.001` suffix is less legible)."""
    if base not in bpy.data.objects:
        return base
    i = 0
    while f"{base}_{i:02d}" in bpy.data.objects:
        i += 1
    return f"{base}_{i:02d}"


def _make_z_of(tile_obj, depsgraph, fallback_z):
    """Return `z_of(x, y)` → tile-LOCAL surface Z (mm) under the local point,
    found by raycasting straight down onto the tile's evaluated mesh
    (BVH-accelerated — fast regardless of how many top faces the tile has).

    A straight-down raycast can genuinely miss at a point that's clearly
    still "on the tile": a Flood Fill region's boundary is walked directly
    off the mesh's own rim vertices, so a seating point can land EXACTLY on
    a real mesh vertex or edge there, a classic degenerate case for a
    ray-triangle intersection test (the ray ends up coplanar with/parallel
    to the near-vertical side wall, or exactly on a vertex several faces
    share). On a miss, `z_of` retries at a small ring of offsets around the
    point (`_MISS_RETRY_DIRECTIONS` directions, escalating through
    `_MISS_RETRY_RADII_MM`) before giving up — cheap, since a miss itself is
    rare and each retry is still one fast BVH raycast, but reliable: for a
    point sitting on a boundary/vertex, at least some of a ring's directions
    point back into the interior of a real face regardless of which way
    "inward" happens to be. Only once every retry also misses does it fall
    back to the fixed `fallback_z` — NOT whatever the *previous* call
    happened to hit: an earlier revision used a closure-captured mutable
    "last successful hit" as the miss fallback, which silently carried a
    stale, unrelated height across calls once any single raycast missed — a
    real, reported bug. (A barycentric-interpolation replacement with no
    raycast-miss case at all was tried next, but its unaccelerated per-call
    scan over every top triangle made a large/Flood-Filled region's full
    placement pass pathologically slow enough to lock up Blender — reverted
    in favour of this retry-ring approach, which keeps the fast BVH raycast;
    see `flagstones.py`'s `_make_corner_sampler` for the identical story.)"""
    down = Vector((0.0, 0.0, -1.0))

    def _cast(x, y):
        hit, loc, _n, _i = tile_obj.ray_cast(
            Vector((x, y, 1.0e5)), down, depsgraph=depsgraph)
        return loc.z if hit else None

    def z_of(x, y):
        z = _cast(x, y)
        if z is not None:
            return z
        for radius in _MISS_RETRY_RADII_MM:
            for i in range(_MISS_RETRY_DIRECTIONS):
                angle = 2.0 * math.pi * i / _MISS_RETRY_DIRECTIONS
                z = _cast(x + radius * math.cos(angle), y + radius * math.sin(angle))
                if z is not None:
                    return z
        return fallback_z

    return z_of


def sync_scatter(tile_obj, region_idx, region_dict, name):
    """(Re)build the joined boulder object for one scatter region.

    `region_dict` is the marshalled region (see `operators.rebuild_tile`) with at
    least `polygon`, `extras` (min/max size, density, distribution) and `seed`.
    The object is named `Boulders_<name>`, parented under `tile_obj`, linked into
    the tile's collection(s) and tagged so a later rebuild/purge can find it.

    Assumes the caller already purged any prior object for this region and ran
    inside `operators._REBUILDING`.
    """
    extras = region_dict.get("extras", {})
    polygon = region_dict.get("polygon") or ps.hex_polygon(
        bpy.context.scene.hexfinity_map.diameter_mm)

    placements = ps.scatter_boulders(
        polygon,
        min_size_mm=extras.get("min_size_mm", 8.0),
        max_size_mm=extras.get("max_size_mm", 40.0),
        density=extras.get("density", 0.6),
        distribution=extras.get("distribution", 0.3),
        seed=region_dict.get("seed", 0),
    )
    if not placements:
        return None

    # Seat the boulders on the real terrain. base_thickness is the lowest the
    # surface can be, so it's a safe fallback for a raycast miss.
    depsgraph = bpy.context.evaluated_depsgraph_get()
    fallback_z = bpy.context.scene.hexfinity_map.base_thickness_mm
    z_of = _make_z_of(tile_obj, depsgraph, fallback_z)

    verts, faces = ps.assemble_scatter_mesh(
        placements, z_of, sink_mm=SINK_MM, roughness=ROUGHNESS, subdiv=SUBDIV)
    assert_two_manifold(verts, faces)

    obj_name = _unique_object_name(f"Boulders_{name}")
    mesh = bpy.data.meshes.new(obj_name)
    mesh.from_pydata(verts, [], faces)
    mesh.update(calc_edges=True)

    obj = bpy.data.objects.new(obj_name, mesh)
    for coll in tile_obj.users_collection:
        coll.objects.link(obj)
    obj.parent = tile_obj            # default matrix_parent_inverse (identity):
    obj.location = (0.0, 0.0, 0.0)   # boulder verts are already in tile-local mm
    obj[SCATTER_OF] = region_key(region_idx)
    obj[SCATTER_MERGE] = bool(region_dict.get("scatter_merge", False))
    return obj


# ---------------------------------------------------------------------------
# Printability — boolean-union the loose boulders into the tile.
# ---------------------------------------------------------------------------
class HEXFINITY_OT_merge_scatter(bpy.types.Operator):
    bl_idname = "hexfinity.merge_scatter"
    bl_label = "Merge Boulders into Tile"
    bl_description = ("Boolean-union this tile's scattered boulder objects into "
                      "the tile mesh so the whole tile prints as one manifold "
                      "piece. Regenerating the tile recreates the loose boulders.")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None and obj.hexfinity_tile.is_generated
                and any(c.get(SCATTER_OF) is not None for c in obj.children))

    def execute(self, context):
        tile_obj = context.active_object
        candidates = [c for c in tile_obj.children
                      if c.get(SCATTER_OF) is not None]
        # Respect per-region "Merge into Tile" if any are flagged; otherwise an
        # explicit button press merges everything.
        flagged = [c for c in candidates if c.get(SCATTER_MERGE)]
        to_merge = flagged or candidates
        if not to_merge:
            self.report({'WARNING'}, "No boulders to merge")
            return {'CANCELLED'}

        for src in to_merge:
            mod = tile_obj.modifiers.new(name="hf_scatter_merge", type='BOOLEAN')
            mod.operation = 'UNION'
            mod.solver = 'EXACT'
            mod.object = src
        with context.temp_override(active_object=tile_obj,
                                   selected_objects=[tile_obj],
                                   object=tile_obj):
            for mod in [m for m in tile_obj.modifiers
                        if m.name.startswith("hf_scatter_merge")]:
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

        self.report({'INFO'}, f"Merged {len(to_merge)} boulder object(s) into tile")
        return {'FINISHED'}
