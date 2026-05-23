import bpy


class HEXFINITY_PT_panel(bpy.types.Panel):
    bl_label = "HexFinity"
    bl_idname = "HEXFINITY_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "HexFinity"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if obj is None or not obj.hexfinity_tile.is_generated:
            layout.label(text="Select a HexTile to edit, or generate a new one.")
            layout.operator("hexfinity.generate", icon='MESH_ICOSPHERE')
            return

        props = obj.hexfinity_tile
        layout.label(text=f"Editing: {obj.name}")

        box = layout.box()
        box.label(text="Base")
        box.prop(props, "diameter_mm")
        box.prop(props, "level_height_mm")
        box.prop(props, "base_thickness_mm")

        box = layout.box()
        box.label(text="Corner Levels (clockwise from top)")
        col = box.column(align=True)
        col.prop(props, "p1")
        col.prop(props, "p2")
        col.prop(props, "p3")
        col.prop(props, "p4")
        col.prop(props, "p5")
        col.prop(props, "p6")

        box = layout.box()
        box.label(text="Center")
        box.prop(props, "override_center")
        row = box.row()
        row.enabled = props.override_center
        row.prop(props, "center_level")
        col = box.column(align=True)
        col.prop(props, "center_x_mm")
        col.prop(props, "center_y_mm")

        box = layout.box()
        box.label(text="Top Surface")
        box.prop(props, "subdivisions")

        layout.operator("hexfinity.generate", icon='MESH_ICOSPHERE')
