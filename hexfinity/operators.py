import math

import bpy
from mathutils import Matrix, Vector

from .mesh_builder import (build_hex_tile, clamp_center_to_hexagon,
                           effective_resample, rim_edge_distance,
                           top_vertex_count)
from .manifold_check import assert_two_manifold, ManifoldError
from .map import SHARED_CORNERS, neighbour_coord, tile_world_xy, find_tile


# Re-entrancy guard: writing clamped XY back to a tile's property group
# re-fires its update callback. Set while a rebuild is already in flight so
# the callback short-circuits.
_REBUILDING = False

# Re-entrancy guard for the corner-level cascade: writing a neighbour's pN
# re-fires its corner callback. Set while the cascade is already in flight
# so the recursive call returns immediately without doing a redundant
# (per-tile) rebuild; the outer cascade batch-rebuilds the affected tiles.
_PROPAGATING = False


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


def rebuild_tile(obj):
    """Rebuild the mesh data of `obj` from its tile props + scene map props.

    Reads `obj.hexfinity_tile` for per-tile inputs (corner levels, centre
    override, centre XY) and `bpy.context.scene.hexfinity_map` for the
    map-wide globals (diameter, level height, base thickness, subdivisions).

    Clamps centre XY into the hexagon and writes the clamped values back so
    the UI and gizmo stay in sync. Does NOT touch `obj.location` — tile
    placement is handled by the caller (generate/regenerate/global update).
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

        # Two per-top-vertex z-offset layers live on the Object and outlive the
        # mesh replaced every rebuild: `hf_brush_disp` (terrain brush) and
        # `hf_snap_disp` (snap-to-model). Both are keyed to the current
        # top-surface topology, so a subdivision/resample change that alters the
        # top vert count invalidates them — drop the stale layer (the agreed
        # "survives height edits only" contract) rather than misapply it. The two
        # layers are summed and applied together.
        resample = _effective_resample(obj, map_props)
        num_top = top_vertex_count(map_props.smoothness_passes, resample)

        def _layer(key):
            arr = obj.get(key)
            if arr is not None and len(arr) != num_top:
                del obj[key]
                if key == "hf_snap_disp":
                    _drop_snap_cache(obj)
                arr = None
            return arr

        brush = _layer("hf_brush_disp")
        snap = _layer("hf_snap_disp")
        obj["hf_brush_ntop"] = num_top

        if brush is None and snap is None:
            combined = None
        else:
            b = brush if brush is not None else [0.0] * num_top
            s = snap if snap is not None else [0.0] * num_top
            combined = [b[i] + s[i] for i in range(num_top)]

        # Procedural surface regions: closed polygons (tile-local mm) + params.
        # Marshalled into plain dicts the bpy-free builder understands. Sampled
        # in global coords (origin = tile world XY) so patterns flow across seams;
        # a per-tile-but-reproducible seed varies the pattern between tiles.
        surface_regions = []
        for reg in tile_props.surface_regions:
            surface_regions.append({
                "surface_type": reg.surface_type,
                "feature_mm": reg.feature_mm,
                "depth_mm": reg.depth_mm,
                "regularity": reg.regularity,
                "direction_rad": math.radians(reg.direction_deg),
                "polygon": [(p.x, p.y) for p in reg.points],
                "mask_falloff_mm": reg.mask_falloff_mm,
            })
        surface_seed = (tile_props.coord_q * 73856093) ^ (tile_props.coord_r * 19349663)

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
        )
        assert_two_manifold(verts, faces)

        new_mesh = bpy.data.meshes.new(obj.name)
        new_mesh.from_pydata(verts, [], faces)
        new_mesh.update(calc_edges=True)

        old_mesh = obj.data
        obj.data = new_mesh
        if old_mesh is not None and old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)
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
    global _PROPAGATING
    if _PROPAGATING:
        # Inside a cascade — the outer call rebuilds all affected tiles
        # after all the neighbour writes have completed.
        return

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


class HEXFINITY_OT_regenerate_map(bpy.types.Operator):
    bl_idname = "hexfinity.regenerate_map"
    bl_label = "Regenerate Map"
    bl_description = ("Delete the existing HexFinity map and rebuild it from "
                      "the current settings. All per-tile edits are lost.")
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
        coll = map_props.root_collection
        if coll is not None:
            # Drop each tile first so removing the collection doesn't leave
            # orphaned objects linked to it in other collections.
            for o in list(coll.objects):
                bpy.data.objects.remove(o, do_unlink=True)
            bpy.data.collections.remove(coll)
        map_props.root_collection = None
        map_props.is_generated = False
        return _build_map(context, self)


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
# Terrain snap-to-model — driven by the per-model `snap_mm` slider
# (properties.HexFinityTerrainProperties.snap_mm update callback).

EMBED_MM = 0.2          # seat the surface this far past the base (overlap)
SNAP_BASE_TOL_MM = 1.0  # an up-hit within this of base_z counts as "flat base"


def _smooth_falloff(t):
    """Smoothstep falloff: 1 at t=0, 0 at t=1, C1 at both ends.

    Mirrors brush._smooth_falloff; kept local so operators doesn't import the
    bpy brush module (which imports back into operators)."""
    s = t * t * (3.0 - 2.0 * t)
    return 1.0 - s


def _drop_snap_cache(tile):
    """Remove the cached snap gap/weight/signature id-properties from `tile`."""
    for k in ("hf_snap_gap", "hf_snap_weight", "hf_snap_sig"):
        if tile.get(k) is not None:
            del tile[k]


def _reset_tile_snap(tile):
    """Strip a tile's snap state — disp layer and gap cache — and rebuild it.
    Used when a model moves off a tile it had snapped.

    Does NOT touch `local_subdiv`: that's a persistent per-tile parameter now,
    not snap state, so a model leaving must not reset the tile's subdivision."""
    changed = False
    if tile.get("hf_snap_disp") is not None:
        del tile["hf_snap_disp"]
        changed = True
    _drop_snap_cache(tile)
    if changed:
        rebuild_tile(tile)


def _snap_signature(model, tile, num_top, target_z):
    """Staleness key for the cached gap pass (a string, for id-prop storage)."""
    p = tile.hexfinity_tile
    mw = tuple(round(c, 4) for row in model.matrix_world for c in row)
    return repr((
        model.name, num_top, round(target_z, 4), mw,
        p.local_subdiv,
        round(model.hexfinity_terrain.snap_damp_mm, 4),
        (p.p1, p.p2, p.p3, p.p4, p.p5, p.p6),
        round(p.center_x_mm, 4), round(p.center_y_mm, 4),
        bool(p.override_center), int(p.center_level),
        round(p.dome_area, 6), round(p.dome_damping, 6),
    ))


def _compute_snap_gap(tile, map_props, model, mmin, mmax, base_z, target_z, num_top):
    """Expensive pass: undisplaced build + per-vertex up-raycast of the model.

    Returns (gap, weight), both length num_top:
    - `gap[i]` is the signed distance `target_z - baseline` (baseline =
      undisplaced top Z + current brush offset), for every near vertex.
    - `weight[i] in [0, 1]`: 1.0 for "core" verts under the model's flat base
      (first up-hit within SNAP_BASE_TOL_MM of base_z — so arch openings and
      overhang shadows stay 0); a smoothstep falloff of the XY distance to the
      nearest core vertex out to `snap_damp_mm` for the organic skirt around the
      footprint; 0 beyond. The skirt is faded toward the rim so a model near a
      hex edge doesn't desync the seam with its neighbour.
    """
    p = tile.hexfinity_tile
    cx, cy = clamp_center_to_hexagon(
        p.center_x_mm, p.center_y_mm, map_props.diameter_mm)
    undisp, _faces = build_hex_tile(
        diameter_mm=map_props.diameter_mm,
        level_height_mm=map_props.level_height_mm,
        base_thickness_mm=map_props.base_thickness_mm,
        corner_levels=(p.p1, p.p2, p.p3, p.p4, p.p5, p.p6),
        center_level=(p.center_level if p.override_center else None),
        smoothness_passes=map_props.smoothness_passes,
        resample_density=_effective_resample(tile, map_props),
        center_xy=(cx, cy),
        dome_area=p.dome_area,
        dome_damping=p.dome_damping,
        top_displacement=None,
    )
    brush = tile.get("hf_brush_disp")
    if brush is None or len(brush) != num_top:
        brush = [0.0] * num_top

    inv = model.matrix_world.inverted()
    up_local = (inv.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
    tx, ty = tile.location.x, tile.location.y
    eps = 1e-4
    gap = [0.0] * num_top
    weight = [0.0] * num_top

    # Core pass: gap for every vert, and the binary footprint via up-raycast.
    core_pts = []  # world XY of verts under the flat base
    for i in range(num_top):
        vx, vy, vz = undisp[i]
        gap[i] = target_z - (vz + brush[i])
        xw, yw = tx + vx, ty + vy
        if (xw < mmin.x - eps or xw > mmax.x + eps
                or yw < mmin.y - eps or yw > mmax.y + eps):
            continue
        hit, loc, _n, _idx = model.ray_cast(
            inv @ Vector((xw, yw, base_z - 2.0)), up_local)
        if not hit:
            continue
        if abs((model.matrix_world @ loc).z - base_z) > SNAP_BASE_TOL_MM:
            continue
        weight[i] = 1.0
        core_pts.append((xw, yw))

    # Skirt pass: smoothstep falloff of the distance to the nearest core vert,
    # out to snap_damp_mm, faded toward the rim. Core verts keep weight 1.
    damp = max(0.0, model.hexfinity_terrain.snap_damp_mm)
    if damp > 0.0 and core_pts:
        diameter = map_props.diameter_mm
        damp2 = damp * damp
        for i in range(num_top):
            if weight[i] >= 1.0:
                continue
            vx, vy, _vz = undisp[i]
            xw, yw = tx + vx, ty + vy
            if (xw < mmin.x - damp or xw > mmax.x + damp
                    or yw < mmin.y - damp or yw > mmax.y + damp):
                continue
            best2 = None
            for (cxw, cyw) in core_pts:
                d2 = (xw - cxw) ** 2 + (yw - cyw) ** 2
                if best2 is None or d2 < best2:
                    best2 = d2
                    if best2 == 0.0:
                        break
            if best2 is None or best2 >= damp2:
                continue
            w = _smooth_falloff((best2 ** 0.5) / damp)
            # Fade the skirt toward the rim so seams with neighbours stay put.
            rim = rim_edge_distance(vx, vy, diameter)
            w *= max(0.0, min(1.0, rim / damp))
            if w > 0.0:
                weight[i] = w
    return gap, weight


def apply_terrain_snap(model, snap_mm):
    """Move the hex top under `model`'s flat base toward it by `snap_mm` mm.

    Absolute and bidirectional: each footprint vertex moves `min(snap_mm, |gap|)`
    toward `target_z = base_z + EMBED_MM` (verts below the base rise, verts above
    descend). Writes the static `hf_snap_disp` layer on the tile (merged with the
    brush layer by `rebuild_tile`) and rebuilds it. The expensive footprint/gap
    pass is cached and reused across slider steps. Off-map / no-tile situations
    are silent no-ops (callbacks can't report).
    """
    map_props = bpy.context.scene.hexfinity_map
    if not map_props.is_generated:
        return
    tprops = model.hexfinity_terrain

    mmin, mmax = _world_bbox([model])
    fc = (mmin + mmax) * 0.5
    tile = _tile_under_point(map_props, fc.x, fc.y)

    # Clear stale snap state left on a tile the model no longer sits over.
    prev = tprops.snap_tile
    if prev is not None and prev is not tile:
        _reset_tile_snap(prev)
    tprops.snap_tile = tile  # PointerProperty has no update hook -> safe
    if tile is None:
        return

    num_top = top_vertex_count(map_props.smoothness_passes,
                               _effective_resample(tile, map_props))
    if len(tile.data.vertices) < num_top:
        return

    # 0 = no snapping: drop the layer and restore the brush-only surface.
    if snap_mm <= 0:
        if tile.get("hf_snap_disp") is not None:
            del tile["hf_snap_disp"]
        rebuild_tile(tile)
        return

    base_z = mmin.z
    # Floor-safe: the builder clamps final Z to >= base_thickness_mm, so a base
    # below the plate can't be reached — target there is the plate itself.
    target_z = max(base_z + EMBED_MM, map_props.base_thickness_mm)

    # Reuse the cached gap/weight unless the model pose, tile shape, or damping
    # changed (snap_damp_mm is part of the signature).
    sig = _snap_signature(model, tile, num_top, target_z)
    gap = tile.get("hf_snap_gap")
    weight = tile.get("hf_snap_weight")
    if (gap is None or weight is None or len(gap) != num_top
            or len(weight) != num_top or tile.get("hf_snap_sig") != sig):
        gap, weight = _compute_snap_gap(
            tile, map_props, model, mmin, mmax, base_z, target_z, num_top)
        tile["hf_snap_gap"] = gap
        tile["hf_snap_weight"] = weight
        tile["hf_snap_sig"] = sig

    s = float(snap_mm)
    snap = [0.0] * num_top
    nonzero = False
    for i in range(num_top):
        w = weight[i]
        if w <= 0.0:
            continue
        g = gap[i]
        snap[i] = w * math.copysign(min(s, abs(g)), g)
        if snap[i] != 0.0:
            nonzero = True
    if nonzero:
        tile["hf_snap_disp"] = snap
    elif tile.get("hf_snap_disp") is not None:
        del tile["hf_snap_disp"]
    rebuild_tile(tile)
