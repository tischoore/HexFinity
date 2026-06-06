import bpy


class HEXFINITY_PT_panel(bpy.types.Panel):
    bl_label = "HexFinity"
    bl_idname = "HEXFINITY_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "HexFinity"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        map_props = scene.hexfinity_map

        # ---- Globals (shown in both branches) -----------------------------
        # These four are uniform across the map by design (their values drive
        # either the grid pitch or the per-tile vertex layout — diverging
        # them per tile would tear the tessellation open).
        box = layout.box()
        box.label(text="Map Globals")
        box.prop(map_props, "diameter_mm")
        box.prop(map_props, "level_height_mm")
        box.prop(map_props, "base_thickness_mm")
        box.prop(map_props, "smoothness_passes")
        box.prop(map_props, "resample_density")

        # ---- Grid extent + (Re)generate ----------------------------------
        box = layout.box()
        box.label(text="Grid")
        row = box.row(align=True)
        row.prop(map_props, "grid_x")
        row.prop(map_props, "grid_y")
        box.label(text="X = 0 or Y = 0 → single tile at (0, 0)", icon='INFO')
        box.prop(map_props, "base_level")
        box.label(text="Base Level applies on (re)generate (wipes edits).",
                  icon='INFO')

        if not map_props.is_generated:
            layout.operator("hexfinity.generate_map", icon='MESH_ICOSPHERE')
            layout.label(text="Select a HexTile after generation to edit it.")
            return

        layout.operator("hexfinity.regenerate_map", icon='FILE_REFRESH')

        # ---- Per-tile section (only when a HexFinity tile is active) -----
        obj = context.active_object
        if obj is None or not obj.hexfinity_tile.is_generated:
            # A non-tile mesh is a dropped terrain object — offer to re-seat it
            # onto the surface of whichever hex it currently sits over.
            if (obj is not None and obj.type == 'MESH'
                    and not obj.hexfinity_tile.is_generated):
                box = layout.box()
                box.label(text=f"Terrain Object: {obj.name}", icon='OBJECT_DATA')
                box.operator("hexfinity.redrop_terrain_object",
                             text="Re-drop onto hex", icon='IMPORT')
                box.prop(obj.hexfinity_terrain, "snap_mm", slider=True)
                box.prop(obj.hexfinity_terrain, "snap_damp_mm", slider=True)
            else:
                layout.label(text="Select a HexTile to edit its corners.")
            return

        tile = obj.hexfinity_tile
        box = layout.box()
        box.label(
            text=f"Editing: {obj.name}   (q={tile.coord_q}, r={tile.coord_r})"
        )

        sub = box.box()
        sub.label(text="Corner Levels (clockwise from upper-right)")
        col = sub.column(align=True)
        col.prop(tile, "p1")
        col.prop(tile, "p2")
        col.prop(tile, "p3")
        col.prop(tile, "p4")
        col.prop(tile, "p5")
        col.prop(tile, "p6")

        sub = box.box()
        sub.label(text="Center")
        sub.prop(tile, "override_center")
        row = sub.row()
        row.enabled = tile.override_center
        row.prop(tile, "center_level")
        col = sub.column(align=True)
        col.prop(tile, "center_x_mm")
        col.prop(tile, "center_y_mm")
        col = sub.column(align=True)
        col.prop(tile, "dome_area", slider=True)
        col.prop(tile, "dome_damping", slider=True)
        col.prop(tile, "local_subdiv")

        box.operator("hexfinity.import_terrain_object",
                     text="Terrain Objects", icon='IMPORT')

        # ---- Terrain Brush ------------------------------------------------
        brush = scene.hexfinity_brush
        box = layout.box()
        box.label(text="Terrain Brush", icon='BRUSH_DATA')
        box.prop(brush, "direction", expand=True)
        col = box.column(align=True)
        col.prop(brush, "radius_mm")
        col.prop(brush, "strength_mm")
        box.prop(brush, "preserve_edge")
        row = box.row()
        row.enabled = brush.preserve_edge
        row.prop(brush, "edge_falloff_mm")
        box.operator("hexfinity.paint_brush", text="Paint", icon='BRUSH_DATA')
        box.label(text="Bump smoothness/resample clears paint.", icon='INFO')
