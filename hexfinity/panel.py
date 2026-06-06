import math

import bpy

from .mesh_builder import effective_resample, top_vertex_count


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
        box.prop(map_props, "man_height_mm")

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

        # Per-tile editing UI (selection-dependent); the export box below it is
        # map-wide, so it always renders at the bottom regardless of selection.
        self._draw_tile_section(context, layout, scene, map_props)

        # ---- Export (map-wide, bottom of the panel) -----------------------
        box = layout.box()
        box.label(text="Export", icon='EXPORT')
        box.operator("hexfinity.export_tiles",
                     text="Export Tiles to STL", icon='EXPORT')
        box.label(text="One STL per distinct tile; identical tiles merge.",
                  icon='INFO')

    def _draw_tile_section(self, context, layout, scene, map_props):
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

        # Capture the active tile's corner levels before any slider edit so the
        # corner callback can recover the pre-edit value and fan its delta out
        # across a multi-selection. Re-seeds only when the active object changes.
        from . import operators
        operators.seed_corner_snapshot_if_changed(obj)

        box = layout.box()
        box.label(
            text=f"Editing: {obj.name}   (q={tile.coord_q}, r={tile.coord_r})"
        )

        sub = box.box()
        sub.label(text="Corner Levels (clockwise from upper-right)")
        selected_tiles = sum(
            1 for o in context.selected_objects if o.hexfinity_tile.is_generated
        )
        if selected_tiles > 1:
            sub.label(text=f"{selected_tiles} tiles selected — edits apply to all",
                      icon='INFO')
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

        # ---- Procedural Surface regions -----------------------------------
        self._draw_surface_regions(context, layout, map_props, obj, tile)

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

    @staticmethod
    def _draw_surface_regions(context, layout, map_props, obj, tile):
        box = layout.box()
        box.label(text="Procedural Surface", icon='TEXTURE')
        box.operator("hexfinity.draw_region", text="Draw Region", icon='GREASEPENCIL')

        row = box.row()
        row.template_list(
            "HEXFINITY_UL_surface_regions", "",
            tile, "surface_regions",
            tile, "active_surface_region_index",
            rows=2,
        )
        col = row.column(align=True)
        col.operator("hexfinity.add_region", text="", icon='ADD')
        col.operator("hexfinity.remove_region", text="", icon='REMOVE')

        idx = tile.active_surface_region_index
        if not (0 <= idx < len(tile.surface_regions)):
            return
        reg = tile.surface_regions[idx]
        from . import procedural_surfaces as ps
        surf = ps.SURFACES.get(reg.surface_type)

        sub = box.column(align=True)
        sub.prop(reg, "name")
        sub.prop(reg, "surface_type")
        if reg.surface_type == 'NONE' or surf is None:
            return

        if surf.kind == 'scatter':
            HEXFINITY_PT_panel._draw_scatter_params(box, reg, surf, map_props)
            return

        # ---- Displacement surface params ----------------------------------
        sub.prop(reg, "mask_falloff_mm")
        if surf.uses_feature:
            sub.prop(reg, "feature_mm")
        sub.prop(reg, "depth_mm")
        sub.prop(reg, "regularity", slider=True)
        if surf.uses_direction:
            sub.prop(reg, "direction_deg")

        # Resolution guidance: warn when the feature is finer than the mesh can
        # resolve (heightfield detail is bounded by the top-vertex spacing).
        if not surf.uses_feature:
            return
        resample = effective_resample(map_props.resample_density, tile.local_subdiv)
        nverts = top_vertex_count(map_props.smoothness_passes, resample)
        R = map_props.diameter_mm * 0.5
        hex_area = (3.0 * math.sqrt(3.0) / 2.0) * R * R
        spacing = math.sqrt(hex_area / max(nverts, 1))
        if reg.feature_mm < 2.0 * spacing:
            box.label(
                text=f"Feature {reg.feature_mm:.1f}mm < 2x vert spacing "
                     f"({spacing:.1f}mm) — raise Local Subdivision",
                icon='ERROR')
        else:
            box.label(text=f"Vert spacing ~{spacing:.1f}mm", icon='INFO')

    @staticmethod
    def _draw_scatter_params(box, reg, surf, map_props):
        # Each scatter knob rides a generic param0..param3 slot; label it from
        # the surface's ParamSpec so the registry stays the single source of
        # truth (no per-surface UI code).
        from . import procedural_surfaces as ps
        from . import scatter
        slots = ("param0", "param1", "param2", "param3")
        col = box.column(align=True)
        vals = {}
        for i, spec in enumerate(surf.extra_params):
            if i >= len(slots):
                break
            col.prop(reg, slots[i], text=spec.label)
            vals[spec.key] = getattr(reg, slots[i])

        # Vertex-budget guidance: density x size over a big region can yield a
        # very heavy joined mesh, so estimate it up front.
        poly = [(p.x, p.y) for p in reg.points] or ps.hex_polygon(map_props.diameter_mm)
        count = ps.estimate_boulder_count(
            poly,
            min_size_mm=vals.get("min_size_mm", 0.0),
            max_size_mm=vals.get("max_size_mm", 0.0),
            density=vals.get("density", 0.0))
        verts_per = len(ps._icosphere(scatter.SUBDIV)[0])
        total = count * verts_per
        msg = f"~{count} boulders (~{total:,} verts)"
        box.label(text=msg,
                  icon='ERROR' if total > 200_000 else 'INFO')

        box.prop(reg, "scatter_merge")
        box.operator("hexfinity.merge_scatter",
                     text="Merge Boulders into Tile", icon='MOD_BOOLEAN')
