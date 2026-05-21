import bpy

from .mesh_builder import build_hex_tile
from .manifold_check import assert_two_manifold, ManifoldError


class HEXFINITY_OT_generate(bpy.types.Operator):
    bl_idname = "hexfinity.generate"
    bl_label = "Generate Tile"
    bl_description = "Generate a new HexFinity tile from the current settings"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.hexfinity
        corner_levels = (props.p1, props.p2, props.p3, props.p4, props.p5, props.p6)
        center_level = props.center_level if props.override_center else None

        try:
            verts, faces = build_hex_tile(
                diameter_mm=props.diameter_mm,
                level_height_mm=props.level_height_mm,
                base_thickness_mm=props.base_thickness_mm,
                corner_levels=corner_levels,
                center_level=center_level,
                subdivisions=props.subdivisions,
            )
            assert_two_manifold(verts, faces)
        except (ValueError, ManifoldError) as exc:
            self.report({'ERROR'}, f"HexFinity: {exc}")
            return {'CANCELLED'}

        mesh = bpy.data.meshes.new("HexTile")
        mesh.from_pydata(verts, [], faces)
        mesh.update(calc_edges=True)

        obj = bpy.data.objects.new("HexTile", mesh)
        context.collection.objects.link(obj)

        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj

        return {'FINISHED'}
