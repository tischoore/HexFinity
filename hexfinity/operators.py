import bpy

from .mesh_builder import build_hex_tile, clamp_center_to_hexagon
from .manifold_check import assert_two_manifold, ManifoldError


# Re-entrancy guard: writing clamped XY back to the property group would
# re-fire its update callback. Set to True while a rebuild is already in
# flight so the callback short-circuits.
_REBUILDING = False


def rebuild_tile(obj, props):
    """Rebuild the mesh data of `obj` from `props`. Raises on failure.

    Clamps center XY into the hexagon and writes the clamped values back to
    the property group so the UI and gizmo stay in sync with what was built.
    """
    global _REBUILDING
    if _REBUILDING:
        return
    _REBUILDING = True
    try:
        cx, cy = clamp_center_to_hexagon(
            props.center_x_mm, props.center_y_mm, props.diameter_mm,
        )
        if cx != props.center_x_mm:
            props.center_x_mm = cx
        if cy != props.center_y_mm:
            props.center_y_mm = cy

        corner_levels = (props.p1, props.p2, props.p3, props.p4, props.p5, props.p6)
        center_level = props.center_level if props.override_center else None

        verts, faces = build_hex_tile(
            diameter_mm=props.diameter_mm,
            level_height_mm=props.level_height_mm,
            base_thickness_mm=props.base_thickness_mm,
            corner_levels=corner_levels,
            center_level=center_level,
            subdivisions=props.subdivisions,
            center_xy=(cx, cy),
        )
        assert_two_manifold(verts, faces)

        new_mesh = bpy.data.meshes.new("HexTile")
        new_mesh.from_pydata(verts, [], faces)
        new_mesh.update(calc_edges=True)

        old_mesh = obj.data
        obj.data = new_mesh
        if old_mesh is not None and old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)
    finally:
        _REBUILDING = False


def rebuild_tile_if_active(context):
    """Rebuild the active HexTile (if any) from the current property values.

    Called from the center_x/y update callback. Silently no-ops if the
    active object is not a HexTile, or if a rebuild error occurs (errors
    propagate as Blender info; we don't want a keystroke to raise).
    """
    obj = context.active_object
    if obj is None or obj.get("hexfinity_tile") != 1:
        return
    props = context.scene.hexfinity
    try:
        rebuild_tile(obj, props)
    except (ValueError, ManifoldError):
        # A bad parameter combination shouldn't crash the property update.
        # The next Generate-Tile click surfaces the same error to the user.
        pass


class HEXFINITY_OT_generate(bpy.types.Operator):
    bl_idname = "hexfinity.generate"
    bl_label = "Generate Tile"
    bl_description = "Generate a new HexFinity tile from the current settings"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.hexfinity

        mesh = bpy.data.meshes.new("HexTile")
        obj = bpy.data.objects.new("HexTile", mesh)
        obj["hexfinity_tile"] = 1
        context.collection.objects.link(obj)

        try:
            rebuild_tile(obj, props)
        except (ValueError, ManifoldError) as exc:
            bpy.data.objects.remove(obj, do_unlink=True)
            self.report({'ERROR'}, f"HexFinity: {exc}")
            return {'CANCELLED'}

        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj

        return {'FINISHED'}
