"""Floating P1..P6 corner labels for selected HexFinity tiles.

Pure viewport overlay: a SpaceView3D POST_PIXEL draw handler that projects
each corner's world position to 2D pixels and draws "P1".."P6" with blf.
The text is rasterised straight into the viewport framebuffer, so it is
non-selectable, has no scene presence, and is not part of renders.
"""

import math

import bpy
import blf
from bpy_extras import view3d_utils
from mathutils import Vector


_HANDLE = None

_FONT_ID = 0
_FONT_SIZE = 14
_TEXT_COLOR = (1.0, 1.0, 1.0, 1.0)
_SHADOW_COLOR = (0.0, 0.0, 0.0, 0.9)


def _tile_corner_world_positions(obj, map_props):
    """Return [(P1_world, ..., P6_world)] for `obj`, hovering one level above.

    Corner XY in tile-local space matches `mesh_builder.py:201-214`:
        angle_i = pi/3 - i*pi/3, P_i = (R cos, R sin)
    Z is base + (level + 1) * level_height — the extra +level_height is the
    "hover above the corner" offset the user asked for.

    Units: the plugin stores mesh data in mm-as-Blender-Units (verts and
    obj.location are fed raw mm — see operators.rebuild_tile and
    map.tile_world_xy), so corner positions are kept in mm here too.
    """
    tile = obj.hexfinity_tile
    R = map_props.diameter_mm * 0.5
    h = map_props.level_height_mm
    base = map_props.base_thickness_mm
    levels = (tile.p1, tile.p2, tile.p3, tile.p4, tile.p5, tile.p6)

    mw = obj.matrix_world
    out = []
    for i in range(6):
        angle = math.pi / 3.0 - i * (math.pi / 3.0)
        x = R * math.cos(angle)
        y = R * math.sin(angle)
        z = base + (max(0, levels[i]) + 1) * h
        out.append(mw @ Vector((x, y, z)))
    return out


def _draw_callback():
    context = bpy.context
    scene = context.scene
    if scene is None:
        return
    map_props = getattr(scene, "hexfinity_map", None)
    if map_props is None or not map_props.is_generated:
        return

    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None:
        return

    selected = context.selected_objects
    if not selected:
        return

    tiles = [o for o in selected if o.hexfinity_tile.is_generated]
    if not tiles:
        return

    blf.size(_FONT_ID, _FONT_SIZE)
    blf.enable(_FONT_ID, blf.SHADOW)
    blf.shadow(_FONT_ID, 3, *_SHADOW_COLOR)
    blf.shadow_offset(_FONT_ID, 1, -1)
    blf.color(_FONT_ID, *_TEXT_COLOR)

    try:
        for obj in tiles:
            corners = _tile_corner_world_positions(obj, map_props)
            for i, world_pos in enumerate(corners):
                px = view3d_utils.location_3d_to_region_2d(region, rv3d, world_pos)
                if px is None:
                    continue
                blf.position(_FONT_ID, px.x, px.y, 0.0)
                blf.draw(_FONT_ID, f"P{i + 1}")
    finally:
        blf.disable(_FONT_ID, blf.SHADOW)


def register():
    global _HANDLE
    if _HANDLE is not None:
        return
    _HANDLE = bpy.types.SpaceView3D.draw_handler_add(
        _draw_callback, (), 'WINDOW', 'POST_PIXEL'
    )


def unregister():
    global _HANDLE
    if _HANDLE is None:
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(_HANDLE, 'WINDOW')
    except (ValueError, RuntimeError):
        pass
    _HANDLE = None
