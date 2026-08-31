import csv
import json
import math
import os

import bpy
from mathutils import Matrix, Vector

from .mesh_builder import (build_hex_tile, clamp_center_to_hexagon,
                           effective_resample, rim_edge_distance,
                           top_vertex_count)
from .manifold_check import assert_two_manifold, ManifoldError
from .map import (SHARED_CORNERS, neighbour_coord, tile_world_xy, find_tile,
                  clamp_level)
from .tile_export import (is_custom_tile, manifest_rows, short_hash,
                          tile_filename, tile_geometry_hash,
                          flora_placement_filename, flora_manifest_rows)


# Re-entrancy guard: writing clamped XY back to a tile's property group
# re-fires its update callback. Set while a rebuild is already in flight so
# the callback short-circuits.
_REBUILDING = False

# Re-entrancy guard for the corner-level cascade: writing a neighbour's pN
# re-fires its corner callback. Set while the cascade is already in flight
# so the recursive call returns immediately without doing a redundant
# (per-tile) rebuild; the outer cascade batch-rebuilds the affected tiles.
_PROPAGATING = False

# Guard for the multi-select parallel edit: when a corner is changed on the
# active tile while several tiles are selected, the originating callback fans
# the same delta out to every other selected tile by writing their pN. Those
# writes re-fire each tile's corner callback; the guard makes those nested
# calls skip the fan-out block (so the delta isn't re-applied) while still
# letting each tile run its own seam cascade. See on_corner_changed.
_MULTI_APPLYING = False

# Snapshot of the active tile's six corner levels, captured before an edit so
# the corner callback can recover the pre-edit value (Blender's update hook
# only exposes the post-edit value). Re-seeded from the panel whenever the
# active object changes, and after every handled edit. `_ACTIVE_SNAPSHOT_KEY`
# records which object it belongs to so a stale snapshot degrades safely to a
# single-tile edit instead of computing a bogus delta.
_ACTIVE_SNAPSHOT = None
_ACTIVE_SNAPSHOT_KEY = None


def _corner_levels(obj):
    """Return the (p1..p6) tuple for a tile object."""
    tp = obj.hexfinity_tile
    return (tp.p1, tp.p2, tp.p3, tp.p4, tp.p5, tp.p6)


def seed_corner_snapshot(obj):
    """Record `obj`'s current corner levels as the baseline for the next edit."""
    global _ACTIVE_SNAPSHOT, _ACTIVE_SNAPSHOT_KEY
    _ACTIVE_SNAPSHOT = _corner_levels(obj)
    _ACTIVE_SNAPSHOT_KEY = obj.as_pointer()


def seed_corner_snapshot_if_changed(obj):
    """Re-seed the snapshot only when the active object differs from the one it
    was captured for. Called from the panel draw, which runs whenever selection
    changes, so the baseline is fresh before the user touches a slider."""
    if obj is None:
        return
    if _ACTIVE_SNAPSHOT_KEY != obj.as_pointer():
        seed_corner_snapshot(obj)


def _selected_other_tiles(active):
    """Generated HexFinity tiles in the current selection, excluding `active`.
    Dropped terrain objects carry hexfinity_tile.is_generated == False, so they
    are filtered out naturally."""
    return [o for o in bpy.context.selected_objects
            if o is not active and o.hexfinity_tile.is_generated]


# ---------------------------------------------------------------------------
# Mesh rebuild — single tile.

def _effective_resample(tile, map_props):
    """Linear-midpoint pass count for `tile`: the map-wide global plus the
    per-tile `local_subdiv` parameter (operators.HexFinityProperties).

    The local bump densifies just this hex (uniformly — no T-junctions); it adds
    resolution, not smoothing. Combine math lives in the bpy-free builder so it's
    unit-testable.
    """
    return effective_resample(map_props.resample_density,
                              tile.hexfinity_tile.local_subdiv)


def rebuild_tile(obj, finalize_flora=False):
    """Rebuild the mesh data of `obj` from its tile props + scene map props.

    Reads `obj.hexfinity_tile` for per-tile inputs (corner levels, centre
    override, centre XY) and `bpy.context.scene.hexfinity_map` for the
    map-wide globals (diameter, level height, base thickness, subdivisions).

    Clamps centre XY into the hexagon and writes the clamped values back so
    the UI and gizmo stay in sync. Does NOT touch `obj.location` — tile
    placement is handled by the caller (generate/regenerate/global update).

    `finalize_flora` gates the pin/notch interlock (see `flora.notch_specs`/
    `flora.sync_flora`, whose pin is a child of its own tree object): cutting
    a socket is expensive enough that it
    must not run on the interactive per-click rebuilds that fire while
    planting trees, nor on any other rebuild trigger (brush, corner edit,
    terrain snap) — it only runs when explicitly finalizing (leaving the
    flora-placement tool, or the manual Finalize Flora button). Every other
    call site passes the default `False`.
    """
    global _REBUILDING
    if _REBUILDING:
        return
    _REBUILDING = True
    try:
        scene = bpy.context.scene
        map_props = scene.hexfinity_map
        tile_props = obj.hexfinity_tile

        cx, cy = clamp_center_to_hexagon(
            tile_props.center_x_mm, tile_props.center_y_mm,
            map_props.diameter_mm,
        )
        if cx != tile_props.center_x_mm:
            tile_props.center_x_mm = cx
        if cy != tile_props.center_y_mm:
            tile_props.center_y_mm = cy

        corner_levels = (tile_props.p1, tile_props.p2, tile_props.p3,
                         tile_props.p4, tile_props.p5, tile_props.p6)
        center_level = tile_props.center_level if tile_props.override_center else None

        # `hf_brush_disp` (terrain brush) is a per-top-vertex z-offset layer
        # that lives on the Object and outlives the mesh replaced every
        # rebuild. It's keyed to the current top-surface topology, so a
        # subdivision/resample change that alters the top vert count
        # invalidates it — drop the stale layer (the agreed "survives height
        # edits only" contract) rather than misapply it.
        resample = _effective_resample(obj, map_props)
        num_top = top_vertex_count(map_props.smoothness_passes, resample)

        brush = obj.get("hf_brush_disp")
        if brush is not None and len(brush) != num_top:
            del obj["hf_brush_disp"]
            brush = None
        obj["hf_brush_ntop"] = num_top
        combined = list(brush) if brush is not None else None

        # Procedural surface regions: closed polygons (tile-local mm) + params.
        # Marshalled into plain dicts the bpy-free builder understands. Sampled
        # in global coords (origin = tile world XY) so patterns flow across seams;
        # a per-tile-but-reproducible seed varies the pattern between tiles.
        from . import procedural_surfaces as ps
        surface_seed = (tile_props.coord_q * 73856093) ^ (tile_props.coord_r * 19349663)
        surface_regions = []
        for idx, reg in enumerate(tile_props.surface_regions):
            # Marshal the registry's extra (surface-specific) params from the
            # generic param0..param3 slots into a named dict the bpy-free /
            # scatter code understands.
            slots = (reg.param0, reg.param1, reg.param2, reg.param3)
            extras = {spec.key: slots[i]
                      for i, spec in enumerate(ps.extra_param_specs(reg.surface_type))
                      if i < len(slots)}
            surface_regions.append({
                "surface_type": reg.surface_type,
                "kind": ps.surface_kind(reg.surface_type),
                "name": reg.name or f"Area {idx + 1}",
                "feature_mm": reg.feature_mm,
                "depth_mm": reg.depth_mm,
                "regularity": reg.regularity,
                "direction_rad": math.radians(reg.direction_deg),
                "polygon": [(p.x, p.y) for p in reg.points],
                "mask_falloff_mm": reg.mask_falloff_mm,
                "extras": extras,
                "scatter_merge": reg.scatter_merge,
                # Per-region seed so distinct regions scatter differently while
                # staying reproducible across rebuilds.
                "seed": surface_seed ^ (idx * 2654435761),
            })

        from . import flora
        flora_pads = flora.pad_specs(obj)
        flora_notches = flora.notch_specs(obj) if finalize_flora else None
        notch_warnings = [] if finalize_flora else None
        notch_ok_indices = [] if finalize_flora else None
        notch_heights = {} if finalize_flora else None
        terrain_pads = terrain_pad_specs(obj)

        from . import path_features
        path_feature_specs = path_features.path_specs(obj)

        verts, faces = build_hex_tile(
            diameter_mm=map_props.diameter_mm,
            level_height_mm=map_props.level_height_mm,
            base_thickness_mm=map_props.base_thickness_mm,
            corner_levels=corner_levels,
            center_level=center_level,
            smoothness_passes=map_props.smoothness_passes,
            resample_density=resample,
            center_xy=(cx, cy),
            dome_area=tile_props.dome_area,
            dome_damping=tile_props.dome_damping,
            top_displacement=combined,
            surface_regions=surface_regions,
            surface_origin_xy=(obj.location.x, obj.location.y),
            surface_seed=surface_seed,
            flora_pads=flora_pads,
            flora_notches=flora_notches,
            flora_notch_warnings=notch_warnings,
            flora_notch_ok_indices=notch_ok_indices,
            flora_notch_heights=notch_heights,
            terrain_pads=terrain_pads,
            path_features=path_feature_specs,
        )
        assert_two_manifold(verts, faces)
        if notch_warnings:
            print(f"HexFinity flora [{obj.name}]: " + "; ".join(notch_warnings))

        new_mesh = bpy.data.meshes.new(obj.name)
        new_mesh.from_pydata(verts, [], faces)
        new_mesh.update(calc_edges=True)

        old_mesh = obj.data
        obj.data = new_mesh
        if old_mesh is not None and old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)

        # Scatter surfaces place distinct objects on top of the finished tile.
        # Regenerate them AFTER the new mesh is assigned + the depsgraph is
        # re-evaluated, so the seating raycast samples the real (current)
        # surface — not stale geometry. Full purge + recreate keeps it robust to
        # region add/remove/reorder; deterministic seeds make rebuilds identical.
        scatter_regions = [(i, rd) for i, rd in enumerate(surface_regions)
                           if rd.get("kind") == 'scatter']
        existing = any(c.get("hf_scatter_of") is not None for c in obj.children)
        if scatter_regions or existing:
            from . import scatter
            scatter.purge_scatter(obj)
            if scatter_regions:
                bpy.context.view_layer.update()
                for i, rd in scatter_regions:
                    scatter.sync_scatter(obj, i, rd, rd["name"])

        # Planted trees, same full-purge-and-resync shape as scatter above:
        # placements are stored data (species/position/rotation/scale), not
        # re-randomized, so a rebuild reproduces the same trees just
        # re-seated to the current surface. Pin objects (children of their
        # tree, not the tile — see `flora.sync_flora`) only ever exist right
        # after a finalize pass: `purge_flora` unconditionally clears every
        # tree (and therefore its pin) on every rebuild, and `sync_flora`
        # only reattaches a pin for a placement whose socket actually got
        # cut this pass (`notch_ok_indices`, empty unless `finalize_flora`).
        flora_placements = tile_props.flora_placements
        has_flora = any(c.get(flora.FLORA_OF) for c in obj.children)
        if len(flora_placements) > 0 or has_flora:
            flora.purge_flora(obj)
            if len(flora_placements) > 0:
                bpy.context.view_layer.update()
                flora.sync_flora(obj, ok_indices=notch_ok_indices,
                                 notch_heights=notch_heights)
    finally:
        _REBUILDING = False


# ---------------------------------------------------------------------------
# Update entry-points called from the property update callbacks.

def on_corner_changed(obj, corner_idx):
    """Propagate a corner-level edit to the (up to two) tiles that share the
    same geometric vertex, then rebuild every affected tile once.

    Edge-of-map corners are propagated silently: a missing neighbour just
    means there is nothing on the other side, which is correct behaviour.
    """
    global _PROPAGATING, _MULTI_APPLYING
    if _PROPAGATING:
        # Inside a cascade — the outer call rebuilds all affected tiles
        # after all the neighbour writes have completed.
        return

    # Multi-select parallel edit: when this is the originating edit (not a
    # nested call from a seam cascade or from the fan-out itself) and more than
    # one tile is selected, apply the same delta to the same corner index on
    # every other selected tile. The fan-out runs *before* this tile's own seam
    # cascade and strictly outside any _PROPAGATING window, so each target tile
    # runs its own seam cascade + rebuild when its callback fires.
    if not _MULTI_APPLYING:
        targets = _selected_other_tiles(obj)
        if targets and _ACTIVE_SNAPSHOT_KEY == obj.as_pointer():
            delta = _corner_levels(obj)[corner_idx] - _ACTIVE_SNAPSHOT[corner_idx]
            if delta != 0:
                attr = f"p{corner_idx + 1}"
                _MULTI_APPLYING = True
                try:
                    for o in targets:
                        cur = getattr(o.hexfinity_tile, attr)
                        setattr(o.hexfinity_tile, attr, clamp_level(cur + delta))
                finally:
                    _MULTI_APPLYING = False
        # Refresh the baseline so a continuous slider drag computes incremental
        # (not cumulative) deltas on each callback.
        seed_corner_snapshot(obj)

    scene = bpy.context.scene
    if not scene.hexfinity_map.is_generated:
        # Defensive: tile flagged is_generated but no map registered. Just
        # rebuild self — propagation has no map to reach into.
        rebuild_tile(obj)
        return

    tile_props = obj.hexfinity_tile
    new_value = (tile_props.p1, tile_props.p2, tile_props.p3,
                 tile_props.p4, tile_props.p5, tile_props.p6)[corner_idx]

    _PROPAGATING = True
    try:
        affected = [obj]
        for (direction, n_corner_idx) in SHARED_CORNERS[corner_idx]:
            nq, nr = neighbour_coord(tile_props.coord_q, tile_props.coord_r, direction)
            n_obj = find_tile(scene, nq, nr)
            if n_obj is None:
                continue
            n_props = n_obj.hexfinity_tile
            attr = f"p{n_corner_idx + 1}"
            if getattr(n_props, attr) != new_value:
                # The write fires the neighbour's corner callback, which sees
                # _PROPAGATING == True and short-circuits — no recursion.
                setattr(n_props, attr, new_value)
            affected.append(n_obj)
        for o in affected:
            rebuild_tile(o)
    finally:
        _PROPAGATING = False


def on_global_update(map_props):
    """Reflect a change to a map-wide global (diameter, level height, base
    thickness, subdivisions) onto every tile in the map.

    Diameter changes alter the grid pitch, so the position of every tile is
    re-derived from (q, r) too. Cheaper to always re-place than to detect
    which global actually changed.
    """
    if not map_props.is_generated:
        return
    coll = map_props.root_collection
    if coll is None:
        return
    for obj in coll.objects:
        tile_props = obj.hexfinity_tile
        if not tile_props.is_generated:
            continue
        x, y = tile_world_xy(tile_props.coord_q, tile_props.coord_r,
                             map_props.diameter_mm)
        obj.location = (x, y, 0.0)
        rebuild_tile(obj)


def _rebuild_flora_tiles(context):
    """Rebuild every tile that has planted trees — used by the flora pad
    property update callbacks (`flatten_base` / `pad_blend_mm`), which change
    every tile's terrain rather than just the one currently selected."""
    map_props = context.scene.hexfinity_map
    if not map_props.is_generated:
        return
    coll = map_props.root_collection
    if coll is None:
        return
    for obj in coll.objects:
        tile_props = obj.hexfinity_tile
        if tile_props.is_generated and len(tile_props.flora_placements) > 0:
            rebuild_tile(obj)


# ---------------------------------------------------------------------------
# Operators.

def _build_map(context, operator):
    """Shared implementation between Generate and Regenerate.

    Reads scene.hexfinity_map for X/Y and globals; creates one new collection
    holding all the tiles; rolls back on any geometry/manifold failure.
    Returns {'FINISHED'} or {'CANCELLED'} — the caller's return value.
    """
    scene = context.scene
    map_props = scene.hexfinity_map

    nx = max(0, map_props.grid_x)
    ny = max(0, map_props.grid_y)
    # Single-tile fallback for X==0 or Y==0 — preserves the original
    # one-click-one-tile workflow even when a map otherwise doesn't make
    # sense.
    if nx == 0 or ny == 0:
        tiles = [(0, 0)]
    else:
        tiles = [(q, r) for q in range(nx) for r in range(ny)]

    coll = bpy.data.collections.new("HexFinity Map")
    scene.collection.children.link(coll)

    created = []
    try:
        for (q, r) in tiles:
            name = f"HexTile_{q:02d}_{r:02d}"
            mesh = bpy.data.meshes.new(name)
            obj = bpy.data.objects.new(name, mesh)
            coll.objects.link(obj)
            tp = obj.hexfinity_tile
            tp.coord_q = q
            tp.coord_r = r
            # Seed every corner (and the centre override, for when it's later
            # toggled on) at the map-wide base level. is_generated is still
            # False here, so the corner/local update callbacks short-circuit —
            # no premature seam propagation; the first rebuild below builds the
            # raised surface directly.
            bl = map_props.base_level
            tp.p1 = tp.p2 = tp.p3 = tp.p4 = tp.p5 = tp.p6 = bl
            tp.center_level = bl
            x, y = tile_world_xy(q, r, map_props.diameter_mm)
            obj.location = (x, y, 0.0)
            rebuild_tile(obj)
            # Mark *after* a successful build so the panel/gizmo only adopt
            # the tile once its mesh actually exists.
            obj.hexfinity_tile.is_generated = True
            created.append(obj)
    except (ValueError, ManifoldError) as exc:
        for o in created:
            bpy.data.objects.remove(o, do_unlink=True)
        bpy.data.collections.remove(coll)
        operator.report({'ERROR'}, f"HexFinity: {exc}")
        return {'CANCELLED'}

    map_props.root_collection = coll
    map_props.is_generated = True

    for o in context.selected_objects:
        o.select_set(False)
    created[0].select_set(True)
    context.view_layer.objects.active = created[0]
    return {'FINISHED'}


class HEXFINITY_OT_generate_map(bpy.types.Operator):
    bl_idname = "hexfinity.generate_map"
    bl_label = "Generate Map"
    bl_description = "Generate the HexFinity map from the current settings"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return not context.scene.hexfinity_map.is_generated

    def execute(self, context):
        return _build_map(context, self)


class HEXFINITY_OT_clear_map(bpy.types.Operator):
    bl_idname = "hexfinity.clear_map"
    bl_label = "Clear Map"
    bl_description = ("Destructively delete the entire HexFinity map — every "
                      "tile, terrain object, scattered boulder and procedural "
                      "surface — and return to the editable Generate state. "
                      "Nothing is rebuilt; all edits are lost.")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.hexfinity_map.is_generated

    def invoke(self, context, event):
        # Built-in Yes/No modal so the user can't lose the map by accident.
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        scene = context.scene
        map_props = scene.hexfinity_map
        # Flora objects live in a Flora sub-collection nested under
        # root_collection, not directly in root_collection.objects (which
        # only lists directly-linked objects) — tear it down separately
        # before the root_collection loop below.
        flora_coll = map_props.flora_collection
        if flora_coll is not None:
            for o in list(flora_coll.objects):
                bpy.data.objects.remove(o, do_unlink=True)
            if flora_coll.name in bpy.data.collections:
                bpy.data.collections.remove(flora_coll)
        map_props.flora_collection = None
        coll = map_props.root_collection
        if coll is not None:
            # Removing each object linked to the collection also clears the
            # terrain imports and scatter boulders — both are linked into the
            # map's root_collection (see import_terrain_object / sync_scatter),
            # so this single pass tears the whole map down. Drop the objects
            # first so removing the collection leaves nothing orphaned.
            for o in list(coll.objects):
                bpy.data.objects.remove(o, do_unlink=True)
            bpy.data.collections.remove(coll)
        map_props.root_collection = None
        map_props.is_generated = False
        # Collapse the (now editable again) globals back to the default state.
        map_props.show_globals = False
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Terrain-object placement helpers (shared by import + re-drop).

def _bbox_of(points):
    """Axis-aligned (min, max) Vectors of a point cloud."""
    mins = Vector((math.inf, math.inf, math.inf))
    maxs = Vector((-math.inf, -math.inf, -math.inf))
    for w in points:
        mins.x, mins.y, mins.z = min(mins.x, w.x), min(mins.y, w.y), min(mins.z, w.z)
        maxs.x, maxs.y, maxs.z = max(maxs.x, w.x), max(maxs.y, w.y), max(maxs.z, w.z)
    return mins, maxs


def _world_bbox(objs, mats=None):
    """Combined world-space AABB (min, max) of `objs`.

    `mats` optionally maps obj -> the matrix_world to measure instead of the
    object's live one, so a caller can size a transform it hasn't applied yet.
    """
    pts = []
    for o in objs:
        m = o.matrix_world if mats is None else mats[o]
        pts.extend(m @ Vector(c) for c in o.bound_box)
    return _bbox_of(pts)


def _ray_origin_z(p, map_props):
    """A Z safely above the tile's highest possible top-surface point.

    Covers corner/centre levels AND the brush displacement layer, which can
    lift a vertex by up to base_thickness_mm above its level height — so the
    down-ray always starts outside the mesh and hits the top first.
    """
    max_level = max(p.p1, p.p2, p.p3, p.p4, p.p5, p.p6,
                    p.center_level if p.override_center else 0)
    return (2.0 * map_props.base_thickness_mm
            + (max_level + 5) * map_props.level_height_mm + 50.0)


def _ray_surface(tile, p, map_props, lx, ly):
    """Cast straight down onto the tile's top at tile-local (lx, ly).

    Returns (world_z, world_unit_normal) on a hit, else None. The normal is
    flipped to point up so callers can treat it as the surface's outward face.
    """
    big_z = _ray_origin_z(p, map_props)
    hit, loc, nrm, _i = tile.ray_cast(
        Vector((lx, ly, big_z)), Vector((0.0, 0.0, -1.0)))
    if not hit:
        return None
    world_z = (tile.matrix_world @ loc).z
    world_n = (tile.matrix_world.to_3x3() @ nrm).normalized()
    if world_n.z < 0.0:
        world_n = -world_n
    return world_z, world_n


def _apex_surface_z(tile, p, map_props):
    """Fallback surface Z under the tile centre (gizmo apex, minus the +1)."""
    if p.override_center:
        level = max(0, p.center_level)
    else:
        level = (p.p1 + p.p2 + p.p3 + p.p4 + p.p5 + p.p6) / 6.0
    apex_local = map_props.base_thickness_mm + level * map_props.level_height_mm
    return (tile.matrix_world
            @ Vector((p.center_x_mm, p.center_y_mm, apex_local))).z


def _surface_z(tile, p, map_props, lx=None, ly=None):
    """World Z of the top surface at tile-local (lx, ly); default tile centre."""
    if lx is None:
        lx = p.center_x_mm
    if ly is None:
        ly = p.center_y_mm
    hit = _ray_surface(tile, p, map_props, lx, ly)
    return hit[0] if hit is not None else _apex_surface_z(tile, p, map_props)


def _averaged_normal(tile, p, map_props, bmin, bmax, grid=3):
    """Unit surface normal averaged over a grid of down-rays across the
    footprint XY [bmin, bmax].

    Smooths out the per-vertex jitter of the Loop-subdivided top so a planted
    object doesn't snap to one noisy face normal. Rays that fall outside the
    hex miss and are skipped; returns world +Z if nothing hit.
    """
    tx, ty = tile.location.x, tile.location.y
    acc = Vector((0.0, 0.0, 0.0))
    span = max(1, grid - 1)
    for i in range(grid):
        for j in range(grid):
            fx = bmin.x + (bmax.x - bmin.x) * (i / span)
            fy = bmin.y + (bmax.y - bmin.y) * (j / span)
            hit = _ray_surface(tile, p, map_props, fx - tx, fy - ty)
            if hit is not None:
                acc += hit[1]
    if acc.length < 1e-6:
        return Vector((0.0, 0.0, 1.0))
    return acc.normalized()


def _bbox_crosses_hex(bmin, bmax, tile, diameter_mm):
    """True if the world XY AABB pokes past any hex edge (tangent allowed)."""
    apothem = (diameter_mm / 2.0) * math.sqrt(3.0) / 2.0
    eps = 1e-4  # let a tangent edge pass without warning
    tx, ty = tile.location.x, tile.location.y  # tiles are translation-only
    corners = (
        (bmin.x - tx, bmin.y - ty), (bmin.x - tx, bmax.y - ty),
        (bmax.x - tx, bmin.y - ty), (bmax.x - tx, bmax.y - ty),
    )
    for (cx, cy) in corners:
        for i in range(6):
            theta = math.pi / 6.0 - i * (math.pi / 3.0)
            if math.cos(theta) * cx + math.sin(theta) * cy > apothem + eps:
                return True
    return False


def _tile_under_point(map_props, x_world, y_world):
    """The map tile whose hexagon the world XY point sits deepest inside.

    Uses the signed rim distance (positive inside): the tiling is gap-free, so
    the point is strictly inside exactly one hex and that tile has the largest
    distance. Off-map points fall back to the nearest tile (all distances
    negative) — the caller's boundary warning then flags it.
    """
    coll = map_props.root_collection
    if coll is None:
        return None
    best, best_d = None, -math.inf
    for obj in coll.objects:
        tp = obj.hexfinity_tile
        if not tp.is_generated:
            continue
        d = rim_edge_distance(x_world - obj.location.x,
                              y_world - obj.location.y, map_props.diameter_mm)
        if d > best_d:
            best, best_d = obj, d
    return best


class HEXFINITY_OT_import_terrain_object(bpy.types.Operator):
    bl_idname = "hexfinity.import_terrain_object"
    bl_label = "Terrain Objects"
    bl_description = ("Import an STL mesh and drop it onto the centre of the "
                      "selected tile, snapping its base flush to the top "
                      "surface. The object is parented to the tile.")
    bl_options = {'REGISTER', 'UNDO'}

    # Populated by the file browser (fileselect_add).
    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.stl", options={'HIDDEN'})
    snap_to_surface: bpy.props.BoolProperty(
        name="Snap to surface",
        description=("Rest the object's bounding-box base flush on the tile's "
                     "top surface. When off, keep the imported Z."),
        default=True,
    )

    @classmethod
    def poll(cls, context):
        sel = context.selected_objects
        return (context.scene.hexfinity_map.is_generated
                and len(sel) == 1
                and sel[0].hexfinity_tile.is_generated)

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        # Shown in the file browser's operator sidebar.
        self.layout.prop(self, "snap_to_surface")

    def execute(self, context):
        scene = context.scene
        map_props = scene.hexfinity_map
        sel = context.selected_objects
        if len(sel) != 1 or not sel[0].hexfinity_tile.is_generated:
            self.report({'ERROR'}, "Select exactly one HexFinity tile first.")
            return {'CANCELLED'}
        tile = sel[0]
        p = tile.hexfinity_tile

        if not self.filepath:
            self.report({'ERROR'}, "No file selected.")
            return {'CANCELLED'}
        if not hasattr(bpy.ops.wm, "stl_import"):
            self.report({'ERROR'},
                        "STL importer (wm.stl_import) unavailable in this build.")
            return {'CANCELLED'}

        before = set(scene.objects)
        try:
            bpy.ops.wm.stl_import(filepath=self.filepath)
        except RuntimeError as exc:
            self.report({'ERROR'}, f"STL import failed: {exc}")
            return {'CANCELLED'}
        imported = [o for o in scene.objects if o not in before]
        if not imported:
            self.report({'ERROR'}, "Import produced no objects.")
            return {'CANCELLED'}
        # Make sure the importer's transforms are flushed into matrix_world
        # before we measure the imported bounding box below.
        context.view_layer.update()

        # Target the hex's geometric centre (local origin), not the displaced
        # apex (center_x_mm/center_y_mm). Tiles carry only translation, so the
        # local origin maps to the hexagon's true centre in world space.
        center_world = tile.matrix_world @ Vector((0.0, 0.0, 0.0))
        bmin, bmax = _world_bbox(imported)
        bcenter = (bmin + bmax) * 0.5

        # XY: align the import's bbox centre over the hex's geometric centre.
        offset = Vector((center_world.x - bcenter.x,
                         center_world.y - bcenter.y, 0.0))
        # Z: rest the bbox base on the surface directly above that centre (the
        # XY shift leaves min-Z unchanged).
        if self.snap_to_surface:
            offset.z = _surface_z(tile, p, map_props, 0.0, 0.0) - bmin.z

        for o in imported:
            o.location = o.location + offset

        # Flush the location change into matrix_world before we read it below.
        # Blender doesn't recompute matrix_world until the view layer updates,
        # so without this the parenting step captures the stale (pre-offset)
        # world transform and snaps the object back to where it was imported.
        context.view_layer.update()

        # Soft boundary check against the placed bbox.
        pmin, pmax = _world_bbox(imported)
        crosses = _bbox_crosses_hex(pmin, pmax, tile, map_props.diameter_mm)

        # Parent to the tile, preserving the world transform we just set.
        inv = tile.matrix_world.inverted()
        for o in imported:
            world = o.matrix_world.copy()
            o.parent = tile
            o.matrix_parent_inverse = inv
            o.matrix_world = world

        # Live beside the tile in the map collection.
        coll = map_props.root_collection
        if coll is not None:
            for o in imported:
                for c in list(o.users_collection):
                    c.objects.unlink(o)
                if o.name not in coll.objects:
                    coll.objects.link(o)

        if crosses:
            self.report({'WARNING'},
                        f"Imported {len(imported)} object(s); extends past the "
                        f"hex boundary of {tile.name}.")
        else:
            self.report({'INFO'},
                        f"Imported {len(imported)} terrain object(s) onto "
                        f"{tile.name}.")
        return {'FINISHED'}


class HEXFINITY_OT_redrop_terrain_object(bpy.types.Operator):
    bl_idname = "hexfinity.redrop_terrain_object"
    bl_label = "Re-drop onto hex"
    bl_description = ("Re-seat the selected terrain object(s) onto the surface "
                      "of the hex they sit over: keep their XY, drop the base "
                      "flush, and (optionally) tilt them to the surface. "
                      "Re-parents to that tile.")
    bl_options = {'REGISTER', 'UNDO'}

    align_to_surface: bpy.props.BoolProperty(
        name="Align to surface",
        description=("Tilt the object's +Z to the surface normal (averaged "
                     "across its footprint) so it sits planted on a slope. "
                     "Heading (yaw) is preserved. When off, only Z is re-seated."),
        default=True,
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.scene.hexfinity_map.is_generated
                and obj is not None and obj.type == 'MESH'
                and not obj.hexfinity_tile.is_generated)

    def draw(self, context):
        self.layout.prop(self, "align_to_surface")

    def execute(self, context):
        scene = context.scene
        map_props = scene.hexfinity_map

        # Re-drop every selected non-tile mesh; fall back to the active object.
        objs = [o for o in context.selected_objects
                if o.type == 'MESH' and not o.hexfinity_tile.is_generated]
        if not objs:
            obj = context.active_object
            if (obj is not None and obj.type == 'MESH'
                    and not obj.hexfinity_tile.is_generated):
                objs = [obj]
        if not objs:
            self.report({'ERROR'}, "Select a dropped terrain object (not a tile).")
            return {'CANCELLED'}

        # Which hex are they over? Use the combined footprint centre.
        bmin, bmax = _world_bbox(objs)
        fc = (bmin + bmax) * 0.5
        tile = _tile_under_point(map_props, fc.x, fc.y)
        if tile is None:
            self.report({'ERROR'}, "No HexFinity tile found under the object.")
            return {'CANCELLED'}
        p = tile.hexfinity_tile

        # Build each object's target world matrix without mutating it yet, so
        # the bbox/normal queries below measure a consistent pre-state.
        mats = {o: o.matrix_world.copy() for o in objs}

        # Rotation: tilt each object's +Z to the averaged surface normal,
        # pivoting about its own footprint centre so its XY stays put. The
        # minimal rotation (rotation_difference) adds no twist, preserving yaw.
        if self.align_to_surface:
            normal = _averaged_normal(tile, p, map_props, bmin, bmax)
            for o in objs:
                M = mats[o]
                omin, omax = _world_bbox([o], {o: M})
                pivot = Matrix.Translation((omin + omax) * 0.5)
                up_cur = (M.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
                rot = up_cur.rotation_difference(normal).to_matrix().to_4x4()
                mats[o] = pivot @ rot @ pivot.inverted() @ M

        # Z: rest the (possibly rotated) combined base flush on the surface
        # directly under the footprint centre.
        rmin, _rmax = _world_bbox(objs, mats)
        surf_z = _surface_z(tile, p, map_props,
                            fc.x - tile.location.x, fc.y - tile.location.y)
        drop = Matrix.Translation((0.0, 0.0, surf_z - rmin.z))
        for o in objs:
            mats[o] = drop @ mats[o]

        # Re-parent to the hex it landed on, preserving that world transform.
        inv = tile.matrix_world.inverted()
        coll = map_props.root_collection
        for o in objs:
            o.parent = tile
            o.matrix_parent_inverse = inv
            o.matrix_world = mats[o]
            if coll is not None and o.name not in coll.objects:
                for c in list(o.users_collection):
                    c.objects.unlink(o)
                coll.objects.link(o)

        # Flush the new transforms before measuring the placed bbox.
        context.view_layer.update()
        fmin, fmax = _world_bbox(objs)
        crosses = _bbox_crosses_hex(fmin, fmax, tile, map_props.diameter_mm)
        if crosses:
            self.report({'WARNING'},
                        f"Re-dropped {len(objs)} object(s) onto {tile.name}; "
                        f"extends past the hex boundary.")
        else:
            self.report({'INFO'},
                        f"Re-dropped {len(objs)} object(s) onto {tile.name}.")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Terrain snap-to-model — the plateau is the only mechanism that deforms the
# hex to match a terrain object (see the "Terrain-object plateau pads"
# section below); this just holds the shared constants/helper it raycasts
# with, plus the `snap_mm`/`snap_damp_mm` update entry point
# (properties.HexFinityTerrainProperties).

EMBED_MM = 0.2          # seat the surface this far past the base (overlap)
SNAP_BASE_TOL_MM = 1.0  # an up-hit within this of base_z counts as "flat base"


def _raycast_under_flat_base(model, inv, up_local, base_z, mmin, mmax, xw, yw, eps=1e-4):
    """True if world XY (xw, yw) is under `model`'s flat base: within its XY
    bbox and an up-raycast against its mesh (in its own local space, via
    `inv`/`up_local`) lands within `SNAP_BASE_TOL_MM` of `base_z`. Excludes
    arch openings and overhang shadows. Used by `_terrain_pad_grid_hits`'s
    per-grid-point footprint classification.
    """
    if (xw < mmin.x - eps or xw > mmax.x + eps
            or yw < mmin.y - eps or yw > mmax.y + eps):
        return False
    hit, loc, _n, _idx = model.ray_cast(
        inv @ Vector((xw, yw, base_z - 2.0)), up_local)
    if not hit:
        return False
    return abs((model.matrix_world @ loc).z - base_z) <= SNAP_BASE_TOL_MM


def on_terrain_snap_changed(model):
    """Handle an edit to `model.hexfinity_terrain.snap_mm`/`snap_damp_mm`.

    Both properties are read directly off `model.hexfinity_terrain` by
    `terrain_pad_specs` (via `_terrain_objects`, which walks the tile's
    children), so there is nothing to compute here beyond finding the hex
    `model` is parented to and rebuilding it — `rebuild_tile` unconditionally
    recomputes the plateau, and its own signature-based cache picks up the
    change. Off-map / unparented situations are silent no-ops (callbacks
    can't report).
    """
    map_props = bpy.context.scene.hexfinity_map
    if not map_props.is_generated:
        return
    tile = model.parent
    if tile is None or not tile.hexfinity_tile.is_generated:
        return
    rebuild_tile(tile)


# ---------------------------------------------------------------------------
# Per-tile STL export.

def _mesh_children(obj):
    """Immediate mesh children of `obj` — the terrain objects parented to a tile
    by the import operator (which parents them one level deep)."""
    return [c for c in obj.children if c.type == 'MESH']


def _terrain_children(obj):
    """`_mesh_children(obj)` minus planted-tree and pin objects — used for the
    tile's own fused STL export now that trees export as their own separate
    (tree+pin) STL files instead of being merged into the tile's triangle
    soup. Scatter boulders and terrain-import objects are unaffected."""
    from . import flora
    return [c for c in _mesh_children(obj)
           if not c.get(flora.FLORA_OF) and not c.get(flora.FLORA_PIN_OF)]


def _terrain_objects(tile):
    """`_terrain_children(tile)` minus scatter boulder objects — the actual
    imported terrain-object meshes carrying `hexfinity_terrain` snap settings,
    the ones `terrain_pad_specs` builds plateau pads for."""
    from . import scatter
    return [c for c in _terrain_children(tile) if c.get(scatter.SCATTER_OF) is None]


def _is_terrain_object(o):
    """True if `o` is an imported terrain-object mesh (not a tile, not a
    planted tree/pin, not a scatter boulder) — the same eligibility
    `_terrain_objects` filters a tile's children down to, but usable directly
    on a candidate object from the current selection."""
    if o.type != 'MESH' or o.hexfinity_tile.is_generated:
        return False
    from . import flora, scatter
    if o.get(flora.FLORA_OF) or o.get(flora.FLORA_PIN_OF):
        return False
    if o.get(scatter.SCATTER_OF) is not None:
        return False
    return True


# ---------------------------------------------------------------------------
# Terrain-object plateau pads — the sole mechanism that deforms a hex to
# match a terrain object's footprint. Reuses the flora "tree pad" mechanism
# (adaptive local refinement + flatten-to-height, see tree_pads.py): a
# terrain object's footprint gets genuine local mesh density under it,
# rather than being limited by the tile's existing top-vertex spacing, and
# the flattened area is an exact geometric constraint (unlike a per-vertex
# displacement, which would only ever be as flat as however many top
# vertices happen to already exist there). See terrain_pads.py for why the
# footprint is tiled into several small pads rather than one big circle —
# a single circle can't exclude overhangs/holes in the model's base the way
# per-point raycast classification can.

TERRAIN_PAD_GRID_SPACING_MM = 5.0  # footprint-classification sample spacing
TERRAIN_PAD_MARGIN = 1.15
TERRAIN_PAD_BLOCK_CELLS = 3        # cells per pad tile (see cluster_grid_hits)


def _terrain_pad_grid_hits(model, tile, base_z, mmin, mmax):
    """(col, row, x, y) tuples — one per grid cell classified as under
    `model`'s flat base (`_raycast_under_flat_base`) — `x`/`y` converted to
    tile-local mm (tiles are translation-only, so this is a plain subtract)."""
    inv = model.matrix_world.inverted()
    up_local = (inv.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
    tx, ty = tile.location.x, tile.location.y
    spacing = TERRAIN_PAD_GRID_SPACING_MM
    cols = max(1, int(math.ceil((mmax.x - mmin.x) / spacing)) + 1)
    rows = max(1, int(math.ceil((mmax.y - mmin.y) / spacing)) + 1)
    hits = []
    for row in range(rows):
        yw = mmin.y + row * spacing
        for col in range(cols):
            xw = mmin.x + col * spacing
            if _raycast_under_flat_base(model, inv, up_local, base_z, mmin, mmax, xw, yw):
                hits.append((col, row, xw - tx, yw - ty))
    return hits


def _terrain_pads_signature(tile):
    """Staleness key for the cached plateau-pad list (a string, for id-prop
    storage) — covers every terrain object on `tile`, since a plateau pad
    tiling changes if ANY terrain object on the tile moves or has its snap
    props changed. Also includes `base_thickness_mm`, since it feeds the
    floor-clamp in each pad's `target_z`."""
    map_props = bpy.context.scene.hexfinity_map
    parts = [tile.hexfinity_tile.local_subdiv, round(map_props.base_thickness_mm, 4)]
    for o in _terrain_objects(tile):
        mw = tuple(round(c, 4) for row in o.matrix_world for c in row)
        parts.append((
            o.name, mw,
            round(o.hexfinity_terrain.snap_mm, 4),
            round(o.hexfinity_terrain.snap_damp_mm, 4),
            len(o.data.vertices) if o.data else 0,
        ))
    return repr(parts)


def _pack_terrain_pads(pads):
    flat = []
    for p in pads:
        flat.extend((p["x"], p["y"], p["radius_mm"], p["blend_mm"], p["z"]))
    return flat


def _unpack_terrain_pads(flat):
    return [
        {"x": flat[i], "y": flat[i + 1], "radius_mm": flat[i + 2],
         "blend_mm": flat[i + 3], "z": flat[i + 4]}
        for i in range(0, len(flat), 5)
    ]


def _compute_terrain_pad_specs(tile):
    map_props = bpy.context.scene.hexfinity_map
    from . import terrain_pads
    pads = []
    for model in _terrain_objects(tile):
        tprops = model.hexfinity_terrain
        if tprops.snap_mm <= 0:
            continue
        mmin, mmax = _world_bbox([model])
        base_z = mmin.z
        target_z = max(base_z + EMBED_MM, map_props.base_thickness_mm)
        hits = _terrain_pad_grid_hits(model, tile, base_z, mmin, mmax)
        if not hits:
            continue
        blend_mm = max(0.0, tprops.snap_damp_mm)
        for pad in terrain_pads.cluster_grid_hits(
                hits, spacing_mm=TERRAIN_PAD_GRID_SPACING_MM,
                margin=TERRAIN_PAD_MARGIN, block_cells=TERRAIN_PAD_BLOCK_CELLS):
            pad["blend_mm"] = blend_mm
            pad["z"] = target_z
            pads.append(pad)
    return pads


def terrain_pad_specs(tile_obj):
    """Plateau-pad specs for `tile_obj`'s terrain objects, ready for
    `mesh_builder.build_hex_tile`'s `terrain_pads` kwarg.

    Returns `[]` immediately — without raycasting — for a tile with no
    terrain objects. Otherwise builds a grid of footprint-classification
    raycasts (the expensive part, via `_raycast_under_flat_base`) per terrain
    object with `snap_mm > 0`, tiles the hits into small circular pads via
    `terrain_pads.cluster_grid_hits`, and stamps each with `blend_mm` = that
    object's own `snap_damp_mm` and `z` = `target_z` = the model's own base
    height plus `EMBED_MM`, floor-clamped to `base_thickness_mm`.

    The raycast pass is cached on `tile_obj` (`hf_terrain_pads`/
    `hf_terrain_pads_sig`), keyed by every terrain object's transform and
    snap settings, so the routine call from `rebuild_tile` (which fires on
    unrelated edits — corner heights, brush strokes, …) doesn't re-raycast
    unless a terrain object actually moved or its snap settings changed.
    """
    if not _terrain_objects(tile_obj):
        return []
    sig = _terrain_pads_signature(tile_obj)
    if tile_obj.get("hf_terrain_pads_sig") == sig:
        cached = tile_obj.get("hf_terrain_pads")
        if cached is not None:
            return _unpack_terrain_pads(cached)
    pads = _compute_terrain_pad_specs(tile_obj)
    tile_obj["hf_terrain_pads"] = _pack_terrain_pads(pads)
    tile_obj["hf_terrain_pads_sig"] = sig
    return pads


def _resolve_selected_hex(context):
    """The hex tile `HEXFINITY_OT_generate_terrain_plateau` should act on:
    the active object itself if it's a generated tile, or its parent tile if
    it's a terrain object. `None` if neither applies."""
    obj = context.active_object
    if obj is None:
        return None
    if obj.hexfinity_tile.is_generated:
        return obj
    if _is_terrain_object(obj) and obj.parent is not None and obj.parent.hexfinity_tile.is_generated:
        return obj.parent
    return None


class HEXFINITY_OT_generate_terrain_plateau(bpy.types.Operator):
    bl_idname = "hexfinity.generate_terrain_plateau"
    bl_label = "Regenerate Plateau"
    bl_description = (
        "Force-regenerate the local-refinement plateau for every terrain "
        "object on the selected hex. The plateau is normally recomputed "
        "automatically and cached — use this to force a fresh pass, e.g. "
        "after editing a terrain object's mesh in place, a change the cache "
        "can't detect on its own."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not context.scene.hexfinity_map.is_generated:
            return False
        tile = _resolve_selected_hex(context)
        return tile is not None and bool(_terrain_objects(tile))

    def execute(self, context):
        tile = _resolve_selected_hex(context)
        if tile is None or not _terrain_objects(tile):
            self.report({'ERROR'},
                        "Select a hex tile (or a terrain object on one) "
                        "that has terrain objects on it.")
            return {'CANCELLED'}

        for key in ("hf_terrain_pads", "hf_terrain_pads_sig"):
            if tile.get(key) is not None:
                del tile[key]
        rebuild_tile(tile)

        packed = tile.get("hf_terrain_pads")
        count = len(packed) // 5 if packed else 0
        if count == 0:
            self.report({'WARNING'},
                        "No flat area found under any terrain object's "
                        f"footprint on {tile.name} (needs an up-raycast hit "
                        f"within {SNAP_BASE_TOL_MM:g} mm of its lowest "
                        "point) — no plateau pads were created, so the mesh "
                        "is unchanged.")
        else:
            self.report({'INFO'},
                        f"Regenerated {count} plateau pad(s) on {tile.name}.")
        return {'FINISHED'}


def _eval_mesh_local(obj, depsgraph, matrix):
    """Evaluated (verts, faces) of `obj`, with every vertex pre-multiplied by
    `matrix`. Pass the identity to read an object's own local mesh, or
    ``tile_world_inv @ child_world`` to land a child in its tile's local space.

    Modifiers are applied via the depsgraph so the exported geometry matches the
    viewport. The temporary evaluated mesh is always freed.
    """
    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    try:
        verts = [tuple(matrix @ v.co) for v in mesh.vertices]
        faces = [tuple(p.vertices) for p in mesh.polygons]
    finally:
        eval_obj.to_mesh_clear()
    return verts, faces


class HEXFINITY_OT_export_tiles(bpy.types.Operator):
    bl_idname = "hexfinity.export_tiles"
    bl_label = "Export Tiles to STL"
    bl_description = ("Export every distinct hex tile (including its parented "
                      "terrain objects, but not planted trees) to an "
                      "individual STL file, plus one more STL per finalized "
                      "planted tree — the tree and its pin merged into a "
                      "single file, flipped as one rigid body so the tree's "
                      "own base (not the pin's tip) sits on the print bed. "
                      "Identical tiles collapse to one file; "
                      "manifest.csv/flora_manifest.csv map coordinates to "
                      "the files they use.")
    bl_options = {'REGISTER'}

    # Populated by the file browser (fileselect_add, DIR_PATH mode).
    directory: bpy.props.StringProperty(subtype='DIR_PATH')
    subfolder: bpy.props.StringProperty(
        name="Subfolder",
        description=("Name of the folder created under the chosen directory to "
                     "receive the STL files and manifest."),
        default="hexfinity_export",
    )

    @classmethod
    def poll(cls, context):
        return context.scene.hexfinity_map.is_generated

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        # Shown in the file browser's operator sidebar.
        self.layout.prop(self, "subfolder")

    def execute(self, context):
        scene = context.scene
        map_props = scene.hexfinity_map

        if not map_props.is_generated or map_props.root_collection is None:
            self.report({'ERROR'}, "Generate a map before exporting.")
            return {'CANCELLED'}
        if not self.directory:
            self.report({'ERROR'}, "No export directory selected.")
            return {'CANCELLED'}
        if not hasattr(bpy.ops.wm, "stl_export"):
            self.report({'ERROR'},
                        "STL exporter (wm.stl_export) unavailable in this build.")
            return {'CANCELLED'}

        out_dir = os.path.join(self.directory, self.subfolder.strip()
                               or "hexfinity_export")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            self.report({'ERROR'}, f"Could not create export folder: {exc}")
            return {'CANCELLED'}

        tiles = [o for o in map_props.root_collection.objects
                 if o.hexfinity_tile.is_generated]
        if not tiles:
            self.report({'ERROR'}, "No HexFinity tiles found to export.")
            return {'CANCELLED'}

        # Make sure all transforms are flushed before we read matrix_world.
        context.view_layer.update()
        depsgraph = context.evaluated_depsgraph_get()

        # Remember and restore the selection/active object around the export.
        prev_selected = list(context.selected_objects)
        prev_active = context.view_layer.objects.active

        from . import flora

        exported_hashes = {}   # geometry hash -> filename already written
        records = []           # one per tile, for the manifest
        flora_records = []     # one per planted tree, for the flora manifest
        written = 0
        flora_written = 0
        unfinalized_tiles = 0

        try:
            for tile in tiles:
                tp = tile.hexfinity_tile
                children = _terrain_children(tile)

                # Hash the built geometry: hex mesh (tile-local) + each child
                # terrain mesh transformed into the tile's local frame.
                hex_verts, hex_faces = _eval_mesh_local(
                    tile, depsgraph, Matrix.Identity(4))
                tile_world_inv = tile.matrix_world.inverted()
                child_meshes = [
                    _eval_mesh_local(
                        c, depsgraph,
                        tile_world_inv @ c.evaluated_get(depsgraph).matrix_world)
                    for c in children
                ]
                digest = tile_geometry_hash(hex_verts, hex_faces, child_meshes)

                custom = is_custom_tile(
                    has_children=bool(children),
                    has_brush=tile.get("hf_brush_disp") is not None,
                    has_snap=bool(tile.get("hf_terrain_pads")),
                    region_count=len(tp.surface_regions),
                )

                if digest in exported_hashes:
                    fname = exported_hashes[digest]
                else:
                    fname = tile_filename(tp.coord_q, tp.coord_r, custom,
                                          short_hash(digest))
                    path = os.path.join(out_dir, fname)
                    self._export_objects(context, [tile] + children, path)
                    exported_hashes[digest] = fname
                    written += 1

                records.append({"q": tp.coord_q, "r": tp.coord_r,
                                "file": fname, "custom": custom})

                # Planted trees export as their own STL, separate from the
                # tile — that's the whole point of the pin/socket interlock:
                # an independently printed part assembled by hand. A pin is
                # parented to its own tree (see `flora.sync_flora`), not the
                # tile, so it's found via the tree's own children. Only
                # placements with a matching pin (i.e. flora was finalized)
                # are exportable; the rest are silently skipped here and
                # rolled into one warning below.
                tree_objs = {c[flora.FLORA_PLACEMENT_INDEX]: c
                            for c in tile.children if c.get(flora.FLORA_OF)}
                unfinalized_here = False

                for idx, tree_obj in sorted(tree_objs.items()):
                    pin_obj = next((c for c in tree_obj.children
                                    if c.get(flora.FLORA_PIN_OF)), None)
                    if pin_obj is None:
                        unfinalized_here = True
                        continue

                    fname = flora_placement_filename(tp.coord_q, tp.coord_r, idx)
                    self._export_flora_pair(
                        context, depsgraph, tree_obj, pin_obj,
                        os.path.join(out_dir, fname))
                    flora_written += 1

                    flora_records.append({"q": tp.coord_q, "r": tp.coord_r,
                                          "index": idx, "file": fname})
                if unfinalized_here:
                    unfinalized_tiles += 1
        finally:
            for o in context.selected_objects:
                o.select_set(False)
            for o in prev_selected:
                o.select_set(True)
            context.view_layer.objects.active = prev_active

        self._write_manifest(out_dir, records)
        if flora_records:
            self._write_flora_manifest(out_dir, flora_records)

        if unfinalized_tiles:
            self.report(
                {'WARNING'},
                f"{unfinalized_tiles} tile(s) have planted trees without a "
                f"finalized pin — run Finalize Flora first to export them.")
        self.report({'INFO'},
                    f"Exported {written} unique tile STL(s) and "
                    f"{flora_written} planted tree(s) (tree+pin merged into "
                    f"one STL each) from {len(tiles)} tile(s) to {out_dir}")
        return {'FINISHED'}

    @staticmethod
    def _export_objects(context, objs, path):
        """Select `objs` and write them to `path` as one STL.

        STL has no object concept, so the hex and its terrain children merge
        into a single triangle soup. The tile's XY is temporarily zeroed so each
        file is centered near the origin; children follow via parenting, and the
        original location is restored in `finally`.
        """
        tile = objs[0]
        saved_location = tile.location.copy()
        for o in context.selected_objects:
            o.select_set(False)
        try:
            tile.location = (0.0, 0.0, 0.0)
            context.view_layer.update()
            for o in objs:
                o.select_set(True)
            context.view_layer.objects.active = tile
            bpy.ops.wm.stl_export(
                filepath=path,
                export_selected_objects=True,
                apply_modifiers=True,
            )
        finally:
            for o in objs:
                o.select_set(False)
            tile.location = saved_location
            context.view_layer.update()

    @staticmethod
    def _export_flora_pair(context, depsgraph, tree_obj, pin_obj, path):
        """Export a planted tree and its paired pin merged into one STL.

        The pin is parented to the tree (`flora.sync_flora`) with a fixed
        local offset/rotation/scale, so rotating/moving only `tree_obj`
        carries the pin along rigidly, unchanged relative to it — the two
        parts are flipped and anchored as a single rigid body, then both are
        selected together and exported in one `wm.stl_export` call. STL has
        no object concept (see `_export_objects`), so the two meshes merge
        into one triangle soup — no boolean/topology fusion, just two
        disjoint shells sharing a file, printable exactly as they were as
        separate files.

        As-authored (tree upright, pin hanging from the tree's base down into
        the tile's socket), the pin's tip is always the combined part's
        lowest point — the worst possible print-bed contact for a thin 2mm
        peg, with the pin pointing down. `tree_obj` is rotated 180° about its
        own local X axis (always a purely horizontal axis regardless of the
        tree's random per-placement yaw, since yaw only rotates about world
        Z) so the whole rigid tree+pin body is turned upside down: whatever
        was the assembly's highest point (the canopy tip) becomes its new
        lowest point, and the pin — previously the lowest point — ends up
        highest, pointing up. Confirmed with the user as the intended
        print orientation, canopy-down included, rather than re-seating the
        tree on its own base independently of the pin.
        """
        saved_rotation = tree_obj.rotation_euler.copy()
        saved_location = tree_obj.location.copy()
        for o in context.selected_objects:
            o.select_set(False)
        try:
            flip = Matrix.Rotation(math.radians(180), 3, 'X')
            tree_obj.rotation_euler = (
                saved_rotation.to_matrix() @ flip).to_euler(tree_obj.rotation_mode)
            context.view_layer.update()

            tree_verts, _ = _eval_mesh_local(
                tree_obj, depsgraph, tree_obj.evaluated_get(depsgraph).matrix_world)
            pin_verts, _ = _eval_mesh_local(
                pin_obj, depsgraph, pin_obj.evaluated_get(depsgraph).matrix_world)
            min_z = min(
                min(v[2] for v in tree_verts),
                min(v[2] for v in pin_verts),
            )
            tree_obj.location.z -= min_z
            context.view_layer.update()

            tree_obj.select_set(True)
            pin_obj.select_set(True)
            context.view_layer.objects.active = tree_obj
            bpy.ops.wm.stl_export(
                filepath=path,
                export_selected_objects=True,
                apply_modifiers=True,
            )
        finally:
            tree_obj.select_set(False)
            pin_obj.select_set(False)
            tree_obj.rotation_euler = saved_rotation
            tree_obj.location = saved_location
            context.view_layer.update()

    @staticmethod
    def _write_manifest(out_dir, records):
        """Write manifest.csv and manifest.json mapping coordinates to files."""
        rows = manifest_rows(records)
        with open(os.path.join(out_dir, "manifest.csv"), "w",
                  newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["q", "r", "file", "custom"])
            writer.writeheader()
            writer.writerows(rows)
        with open(os.path.join(out_dir, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2)

    @staticmethod
    def _write_flora_manifest(out_dir, records):
        """Write flora_manifest.csv and flora_manifest.json mapping each
        planted tree's (coordinate, placement index) to its merged
        tree+pin STL file."""
        rows = flora_manifest_rows(records)
        with open(os.path.join(out_dir, "flora_manifest.csv"), "w",
                  newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=["q", "r", "index", "file"])
            writer.writeheader()
            writer.writerows(rows)
        with open(os.path.join(out_dir, "flora_manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2)
