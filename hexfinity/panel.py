import math

import bpy

from . import flora
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

        # ---- Pre-map: editable globals + grid + Generate ------------------
        if not map_props.is_generated:
            self._draw_globals(layout, map_props, enabled=True)
            self._draw_grid(layout, map_props, enabled=True)
            layout.operator("hexfinity.generate_map", icon='MESH_ICOSPHERE')
            layout.label(text="Select a HexTile after generation to edit it.")
            return

        # ---- Post-map: Clear + collapsed read-only settings ---------------
        # Once a map exists the generate-time globals are locked (editing them
        # would force a map-wide rebuild) and auto-collapsed to free space; the
        # only action is the destructive Clear, which returns to the pre-map
        # state where the globals become editable again.
        layout.operator("hexfinity.clear_map", icon='TRASH')

        box = layout.box()
        box.prop(map_props, "show_globals",
                 icon='TRIA_DOWN' if map_props.show_globals else 'TRIA_RIGHT',
                 emboss=False, text="Map Settings (read-only)")
        if map_props.show_globals:
            self._draw_globals(box, map_props, enabled=False)
            self._draw_grid(box, map_props, enabled=False)

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

    @staticmethod
    def _draw_globals(parent, map_props, enabled):
        # The map-wide mesh globals. Uniform across the map by design (their
        # values drive either the grid pitch or the per-tile vertex layout —
        # diverging them per tile would tear the tessellation open). Drawn
        # editable before generation, disabled (read-only) afterwards.
        box = parent.box()
        box.label(text="Map Globals")
        col = box.column()
        col.enabled = enabled
        col.prop(map_props, "diameter_mm")
        col.prop(map_props, "level_height_mm")
        col.prop(map_props, "base_thickness_mm")
        col.prop(map_props, "smoothness_passes")
        col.prop(map_props, "resample_density")
        col.prop(map_props, "man_height_mm")

    @staticmethod
    def _draw_grid(parent, map_props, enabled):
        # Grid extent + base level — only take effect on Generate, so they are
        # locked (read-only) once a map exists.
        box = parent.box()
        box.label(text="Grid")
        col = box.column()
        col.enabled = enabled
        row = col.row(align=True)
        row.prop(map_props, "grid_x")
        row.prop(map_props, "grid_y")
        col.label(text="X = 0 or Y = 0 → single tile at (0, 0)", icon='INFO')
        col.prop(map_props, "base_level")
        col.label(text="Base Level applies on generate (wipes edits).",
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
                box.operator("hexfinity.split_terrain_by_hex",
                             text="Split by Hex Boundaries", icon='MOD_BOOLEAN')
                box.prop(obj.hexfinity_terrain, "snap_mm", slider=True)
                box.prop(obj.hexfinity_terrain, "snap_damp_mm", slider=True)
                row = box.row()
                row.enabled = obj.hexfinity_terrain.snap_mm > 0
                row.operator("hexfinity.generate_terrain_plateau",
                             text="Regenerate Plateau", icon='FILE_REFRESH')
                if obj.hexfinity_terrain.snap_mm <= 0:
                    box.label(text="Raise Terrain snap to model above 0 first.",
                             icon='INFO')
            else:
                layout.label(text="Select a HexTile to edit its corners.")
            return

        tile = obj.hexfinity_tile

        # Capture the active tile's corner levels before any slider edit so the
        # corner callback can recover the pre-edit value and fan its delta out
        # across a multi-selection. Re-seeds only when the active object changes.
        from . import operators
        operators.seed_corner_snapshot_if_changed(obj)

        header, box = layout.panel("hexfinity_editing", default_closed=False)
        header.label(
            text=f"Editing: {obj.name}   (q={tile.coord_q}, r={tile.coord_r})"
        )
        if box:
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

        # ---- Terrain Objects --------------------------------------------------
        header, terrain_box = layout.panel("hexfinity_terrain_objects", default_closed=True)
        header.label(text="Terrain Objects", icon='IMPORT')
        if terrain_box:
            terrain_box.operator("hexfinity.import_terrain_object",
                                 text="Import STL", icon='IMPORT')
            if operators._terrain_objects(obj):
                terrain_box.operator("hexfinity.generate_terrain_plateau",
                                     text="Regenerate Plateau", icon='FILE_REFRESH')

        # ---- Flora ----------------------------------------------------------
        header, flora_box = layout.panel("hexfinity_flora", default_closed=True)
        header.label(text="Flora", icon='OUTLINER_OB_POINTCLOUD')
        if flora_box:
            flora_box.prop(scene.hexfinity_flora, "tree_type")
            flora_box.prop(scene.hexfinity_flora, "scale_variation_pct")
            flora_box.prop(scene.hexfinity_flora, "flatten_base")
            row = flora_box.row()
            row.enabled = scene.hexfinity_flora.flatten_base
            row.prop(scene.hexfinity_flora, "pad_blend_mm")
            if scene.hexfinity_flora.flatten_base:
                flora_box.label(
                    text="Flatten adds a few extra verts near each tree.",
                    icon='INFO')
            flora_box.prop(scene.hexfinity_flora, "penetration_mm")
            flora_box.prop(scene.hexfinity_flora, "avoid_overlap")
            row = flora_box.row()
            row.enabled = scene.hexfinity_flora.avoid_overlap
            row.prop(scene.hexfinity_flora, "min_spacing_mm")
            if flora.is_active():
                row = flora_box.row()
                row.alert = True
                row.label(text="Flora active — Esc / RMB to close", icon='INFO')
            else:
                flora_box.operator("hexfinity.flora_marker", text="Flora",
                                   icon='OUTLINER_OB_POINTCLOUD')
            flora_box.operator("hexfinity.finalize_flora", text="Finalize Flora",
                               icon='MOD_SCREW')
            flora_box.label(
                text="Pins/notches only exist right after Finalize — any later "
                    "edit strips them again.", icon='INFO')

        # ---- Surface Texture (whole-tile base layer) -----------------------
        self._draw_surface_texture(context, layout, map_props, tile)

        # ---- Procedural Surface regions -----------------------------------
        self._draw_surface_regions(context, layout, map_props, obj, tile)

        # ---- Path Feature ---------------------------------------------------
        self._draw_path_features(context, layout, scene, tile)

        # ---- Terrain Brush ------------------------------------------------
        brush = scene.hexfinity_brush
        header, box = layout.panel("hexfinity_terrain_brush", default_closed=True)
        header.label(text="Terrain Brush", icon='BRUSH_DATA')
        if box:
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

        # ---- Bake -----------------------------------------------------
        header, box = layout.panel("hexfinity_bake", default_closed=True)
        header.label(text="Bake", icon='NODETREE')
        if box:
            if tile.is_baked:
                box.operator("hexfinity.unbake_tile", text="Un-bake Tile",
                             icon='LOOP_BACK')
                box.label(text="Pad/terrain/notch/path/region-subdiv/brush "
                                "layers are frozen into the mesh.", icon='INFO')
            else:
                box.operator("hexfinity.bake_tile", text="Bake Tile",
                             icon='NODETREE')
            box.label(text="Draw Area/Surface Texture VALUES stay live either "
                            "way; a region's own Local Subdivision geometry "
                            "freezes with the rest.", icon='INFO')
            box.label(text="A corner/dome/global edit auto-reverts the frozen "
                            "pad/terrain/notch/path/region layer (not the "
                            "brush).", icon='INFO')

    @staticmethod
    def _draw_surface_regions(context, layout, map_props, obj, tile):
        header, box = layout.panel("hexfinity_procedural_surface", default_closed=True)
        header.label(text="Procedural Surface", icon='TEXTURE')
        if not box:
            return
        box.operator("hexfinity.draw_region", text="Draw Region", icon='GREASEPENCIL')
        box.operator("hexfinity.flood_fill_region", text="Flood Fill", icon='UV_SYNC_SELECT')
        box.prop(context.scene.hexfinity_flood_fill, "angle_threshold_deg", text="Angle Tolerance")

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

        HEXFINITY_PT_panel._draw_displacement_params(
            box, sub, reg, surf, map_props, tile, show_mask_falloff=True)

    @staticmethod
    def _draw_displacement_params(box, sub, reg, surf, map_props, tile,
                                  show_mask_falloff):
        # ---- Displacement surface params ----------------------------------
        if show_mask_falloff:
            sub.prop(reg, "mask_falloff_mm")
            sub.prop(reg, "local_subdiv")
        if surf.uses_feature:
            sub.prop(reg, "feature_mm")
        sub.prop(reg, "depth_mm")
        sub.prop(reg, "regularity", slider=True)
        if surf.uses_direction:
            sub.prop(reg, "direction_deg")

        # Resolution guidance: warn when the feature is finer than the mesh can
        # resolve (heightfield detail is bounded by the top-vertex spacing). A
        # Draw Area's own Local Subdivision (show_mask_falloff=True) locally
        # halves that spacing per pass, same as `refine_regions`' pass shape.
        if not surf.uses_feature:
            return
        resample = effective_resample(map_props.resample_density, tile.local_subdiv)
        nverts = top_vertex_count(map_props.smoothness_passes, resample)
        R = map_props.diameter_mm * 0.5
        hex_area = (3.0 * math.sqrt(3.0) / 2.0) * R * R
        spacing = math.sqrt(hex_area / max(nverts, 1))
        if show_mask_falloff:
            spacing = spacing / (2 ** max(0, reg.local_subdiv))
            hint = "raise this region's own Local Subdivision"
        else:
            hint = "raise tile-wide Local Subdivision"
        if reg.feature_mm < 2.0 * spacing:
            box.label(
                text=f"Feature {reg.feature_mm:.1f}mm < 2x vert spacing "
                     f"({spacing:.1f}mm) — {hint}",
                icon='ERROR')
        else:
            box.label(text=f"Vert spacing ~{spacing:.1f}mm", icon='INFO')

    @staticmethod
    def _draw_surface_texture(context, layout, map_props, tile):
        header, box = layout.panel("hexfinity_surface_texture", default_closed=True)
        header.label(text="Surface Texture", icon='MOD_NOISE')
        if not box:
            return
        reg = tile.surface_texture
        box.prop(reg, "surface_type", text="")
        if reg.surface_type != 'NONE':
            from . import procedural_surfaces as ps
            surf = ps.SURFACES.get(reg.surface_type)
            if surf is not None:
                sub = box.column(align=True)
                if surf.kind == 'scatter':
                    HEXFINITY_PT_panel._draw_scatter_params(box, reg, surf, map_props)
                else:
                    HEXFINITY_PT_panel._draw_displacement_params(
                        box, sub, reg, surf, map_props, tile, show_mask_falloff=False)

        row = box.row(align=True)
        row.operator("hexfinity.copy_surface_texture", text="Copy Settings", icon='COPYDOWN')
        row.operator("hexfinity.apply_surface_texture", text="Apply", icon='PASTEDOWN')

    @staticmethod
    def _draw_path_features(context, layout, scene, tile):
        tool = scene.hexfinity_path_features
        header, box = layout.panel("hexfinity_path_feature", default_closed=True)
        header.label(text="Path Feature", icon='MOD_CURVE')
        if not box:
            return
        box.prop(tool, "edge_snap")
        box.operator("hexfinity.draw_path_feature", text="Draw Feature",
                     icon='GREASEPENCIL')

        row = box.row()
        row.template_list(
            "HEXFINITY_UL_path_features", "",
            tile, "path_features",
            tile, "active_path_feature_index",
            rows=2,
        )
        col = row.column(align=True)
        col.operator("hexfinity.remove_path_feature", text="", icon='REMOVE')

        idx = tile.active_path_feature_index
        if 0 <= idx < len(tile.path_features):
            feature = tile.path_features[idx]
            sub = box.column(align=True)
            sub.prop(feature, "name")
            sub.prop(feature, "feature_type")
            sub.prop(feature, "width_mm")
            if feature.feature_type == 'RIVER':
                sub.prop(feature, "depth_levels")
                sub.prop(feature, "embankment_angle_deg")
                sub.prop(feature, "embankment_variation_mm")
                sub.prop(feature, "river_bottom_style")
                sub.prop(feature, "local_subdiv")
                box.label(text="To continue into a neighbouring tile, "
                                "lower the shared corner Level(s) at the "
                                "crossing edge to at least Depth.",
                          icon='INFO')
            else:
                sub.prop(feature, "depth_mm")
                sub.prop(feature, "repeat_mm")
                sub.prop(feature, "local_subdiv")

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
