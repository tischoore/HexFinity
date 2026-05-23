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


class HEXFINITY_OT_generate(bpy.types.Operator):
    bl_idname = "hexfinity.generate"
    bl_label = "Generate Tile"
    bl_description = "Generate a new HexFinity tile from the current settings"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        mesh = bpy.data.meshes.new("HexTile")
        obj = bpy.data.objects.new("HexTile", mesh)
        context.collection.objects.link(obj)

        # First access auto-initializes the per-Object PropertyGroup with
        # field defaults.
        try:
            rebuild_tile(obj, obj.hexfinity_tile)
        except (ValueError, ManifoldError) as exc:
            bpy.data.objects.remove(obj, do_unlink=True)
            self.report({'ERROR'}, f"HexFinity: {exc}")
            return {'CANCELLED'}

        # Mark *after* a successful build so the panel/gizmo only adopt the
        # tile once it actually exists. is_generated has no update= callback,
        # so this write does not trigger an extra rebuild.
        obj.hexfinity_tile.is_generated = True

        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj

        return {'FINISHED'}
