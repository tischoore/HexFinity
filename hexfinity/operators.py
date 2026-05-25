import bpy

from .mesh_builder import build_hex_tile, clamp_center_to_hexagon
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

        verts, faces = build_hex_tile(
            diameter_mm=map_props.diameter_mm,
            level_height_mm=map_props.level_height_mm,
            base_thickness_mm=map_props.base_thickness_mm,
            corner_levels=corner_levels,
            center_level=center_level,
            subdivisions=map_props.subdivisions,
            center_xy=(cx, cy),
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
            obj.hexfinity_tile.coord_q = q
            obj.hexfinity_tile.coord_r = r
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
