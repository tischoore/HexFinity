"""HexFinity — modular hexagonal terrain map generator for Blender 5.1.

Packaged as a Blender extension (see blender_manifest.toml). The bpy imports
live in `properties`, `operators`, `panel`, and `gizmo` and are only loaded
from within `register()`, so `mesh_builder`, `manifold_check`, and `map`
remain importable from plain CPython for unit tests.
"""


def _classes():
    from . import (properties, operators, panel, gizmo, brush, regions,
                   scatter, flora, path_features, terrain_lock, segments,
                   segment_path)
    return (
        properties.HexFinityMapProperties,
        properties.HexFinitySurfacePoint,
        properties.HexFinitySurfaceRegion,
        properties.HexFinityFloraPlacement,
        properties.HexFinityPathFeature,
        properties.HexFinityProperties,
        properties.HexFinityBrushProperties,
        properties.HexFinityFloraProperties,
        properties.HexFinityTerrainProperties,
        properties.HexFinityPathFeatureProperties,
        properties.HexFinityFloodFillProperties,
        properties.HexFinitySegmentWaypoint,
        properties.HexFinitySegmentAuthoring,
        properties.HexFinitySegmentsProperties,
        operators.HEXFINITY_OT_generate_map,
        operators.HEXFINITY_OT_clear_map,
        operators.HEXFINITY_OT_add_adjacent_hex,
        operators.HEXFINITY_OT_import_terrain_object,
        operators.HEXFINITY_OT_redrop_terrain_object,
        operators.HEXFINITY_OT_generate_terrain_plateau,
        operators.HEXFINITY_OT_split_terrain_by_hex,
        terrain_lock.HEXFINITY_OT_start_conform_lattice,
        terrain_lock.HEXFINITY_OT_apply_conform_lattice,
        terrain_lock.HEXFINITY_OT_cancel_conform_lattice,
        operators.HEXFINITY_OT_bake_tile,
        operators.HEXFINITY_OT_unbake_tile,
        operators.HEXFINITY_OT_export_tiles,
        operators.HEXFINITY_OT_copy_surface_texture,
        operators.HEXFINITY_OT_apply_surface_texture,
        brush.HEXFINITY_OT_paint_brush,
        flora.HEXFINITY_OT_flora_marker,
        flora.HEXFINITY_OT_finalize_flora,
        regions.HEXFINITY_OT_draw_region,
        regions.HEXFINITY_OT_flood_fill_region,
        regions.HEXFINITY_OT_add_region,
        regions.HEXFINITY_OT_remove_region,
        regions.HEXFINITY_UL_surface_regions,
        path_features.HEXFINITY_OT_draw_path_feature,
        path_features.HEXFINITY_OT_remove_path_feature,
        path_features.HEXFINITY_OT_link_connected_paths,
        path_features.HEXFINITY_UL_path_features,
        scatter.HEXFINITY_OT_merge_scatter,
        segments.HEXFINITY_OT_add_segment_type,
        segments.HEXFINITY_OT_confirm_add_segment_type,
        segments.HEXFINITY_OT_segment_type_info,
        segments.HEXFINITY_OT_draw_segment_path,
        segments.HEXFINITY_OT_finish_add_segment,
        segments.HEXFINITY_OT_cancel_add_segment,
        segment_path.HEXFINITY_OT_cancel_segments_path_dialog,
        segment_path.HEXFINITY_OT_draw_segments_path_dialog,
        segment_path.HEXFINITY_OT_start_segments_path_draw,
        panel.HEXFINITY_PT_panel,
        gizmo.HEXFINITY_GT_center_sphere,
        gizmo.HEXFINITY_GGT_center,
        gizmo.HEXFINITY_GT_add_hex,
        gizmo.HEXFINITY_GGT_add_hex,
    )


def register():
    import bpy
    from . import properties, overlay, segments
    for cls in _classes():
        bpy.utils.register_class(cls)
    bpy.types.Scene.hexfinity_map = bpy.props.PointerProperty(
        type=properties.HexFinityMapProperties
    )
    bpy.types.Scene.hexfinity_brush = bpy.props.PointerProperty(
        type=properties.HexFinityBrushProperties
    )
    bpy.types.Scene.hexfinity_flora = bpy.props.PointerProperty(
        type=properties.HexFinityFloraProperties
    )
    bpy.types.Scene.hexfinity_path_features = bpy.props.PointerProperty(
        type=properties.HexFinityPathFeatureProperties
    )
    bpy.types.Scene.hexfinity_flood_fill = bpy.props.PointerProperty(
        type=properties.HexFinityFloodFillProperties
    )
    bpy.types.Scene.hexfinity_segments = bpy.props.PointerProperty(
        type=properties.HexFinitySegmentsProperties
    )
    bpy.types.Object.hexfinity_tile = bpy.props.PointerProperty(
        type=properties.HexFinityProperties
    )
    bpy.types.Object.hexfinity_terrain = bpy.props.PointerProperty(
        type=properties.HexFinityTerrainProperties
    )
    bpy.types.Object.hexfinity_segment = bpy.props.PointerProperty(
        type=properties.HexFinitySegmentAuthoring
    )
    overlay.register()
    try:
        segments.ensure_settings_file()
    except Exception as exc:
        # Never let a settings.json resolution/creation failure (e.g. an
        # unusual load context where bpy.utils.extension_path_user can't
        # resolve this package's name, or a permissions error) block the
        # rest of the extension from registering — the Add Path Segment
        # Type workflow degrades gracefully (its own operators still raise
        # normally if actually invoked), everything else is unaffected.
        print(f"HexFinity: could not initialize settings.json: {exc}")


def unregister():
    import bpy
    from . import overlay
    overlay.unregister()
    if hasattr(bpy.types.Object, "hexfinity_segment"):
        del bpy.types.Object.hexfinity_segment
    if hasattr(bpy.types.Object, "hexfinity_terrain"):
        del bpy.types.Object.hexfinity_terrain
    if hasattr(bpy.types.Object, "hexfinity_tile"):
        del bpy.types.Object.hexfinity_tile
    if hasattr(bpy.types.Scene, "hexfinity_segments"):
        del bpy.types.Scene.hexfinity_segments
    if hasattr(bpy.types.Scene, "hexfinity_flood_fill"):
        del bpy.types.Scene.hexfinity_flood_fill
    if hasattr(bpy.types.Scene, "hexfinity_path_features"):
        del bpy.types.Scene.hexfinity_path_features
    if hasattr(bpy.types.Scene, "hexfinity_flora"):
        del bpy.types.Scene.hexfinity_flora
    if hasattr(bpy.types.Scene, "hexfinity_brush"):
        del bpy.types.Scene.hexfinity_brush
    if hasattr(bpy.types.Scene, "hexfinity_map"):
        del bpy.types.Scene.hexfinity_map
    for cls in reversed(_classes()):
        bpy.utils.unregister_class(cls)
