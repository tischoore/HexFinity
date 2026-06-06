"""Floating P1..P6 corner labels for selected HexFinity tiles.

Pure viewport overlay: a SpaceView3D POST_PIXEL draw handler that projects
each corner's world position to 2D pixels and draws "P1".."P6" with blf.
The text is rasterised straight into the viewport framebuffer, so it is
non-selectable, has no scene presence, and is not part of renders.
"""

import math

import bpy
import blf
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from mathutils import Vector


_HANDLE = None

_REGION_COLOR = (1.0, 0.8, 0.2, 0.9)
_DIRECTION_COLOR = (0.3, 1.0, 0.5, 0.95)

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


def _region_hover_z(obj, map_props):
    """A z (mm, tile-local) just above the tile top to float the region guide on."""
    tile = obj.hexfinity_tile
    levels = (tile.p1, tile.p2, tile.p3, tile.p4, tile.p5, tile.p6)
    return map_props.base_thickness_mm + (max(levels) + 1) * map_props.level_height_mm


def _draw_regions(context, region, rv3d, map_props, tiles):
    """Draw each tile's region loops on the surface, plus a direction arrow for
    the active region of the active tile (anisotropic surfaces use it)."""
    active_obj = context.active_object
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(2.0)
    shader.bind()

    for obj in tiles:
        tile = obj.hexfinity_tile
        if not len(tile.surface_regions):
            continue
        mw = obj.matrix_world
        z = _region_hover_z(obj, map_props)
        for ri, reg in enumerate(tile.surface_regions):
            pts = reg.points
            if len(pts) < 3:
                continue
            loop = []
            for p in pts:
                s = view3d_utils.location_3d_to_region_2d(
                    region, rv3d, mw @ Vector((p.x, p.y, z)))
                if s is not None:
                    loop.append((s.x, s.y))
            if len(loop) < 2:
                continue
            loop.append(loop[0])
            shader.uniform_float("color", _REGION_COLOR)
            batch_for_shader(shader, 'LINE_STRIP', {"pos": loop}).draw(shader)

            # Direction arrow for the active region of the active tile — only
            # for surfaces that actually use a direction (e.g. furrows), not
            # isotropic or scatter surfaces.
            from . import procedural_surfaces as ps
            surf = ps.SURFACES.get(reg.surface_type)
            is_active = (obj == active_obj
                         and ri == tile.active_surface_region_index
                         and surf is not None and surf.uses_direction)
            if is_active:
                cx = sum(p.x for p in pts) / len(pts)
                cy = sum(p.y for p in pts) / len(pts)
                length = map_props.diameter_mm * 0.18
                ang = math.radians(reg.direction_deg)
                dx, dy = math.cos(ang), math.sin(ang)
                tip = (cx + dx * length, cy + dy * length)
                # Two short barbs for an arrowhead.
                back = length * 0.3
                bx, by = -dx * back, -dy * back
                px, py = -dy * back * 0.6, dx * back * 0.6
                seg = [
                    (cx, cy, z), (tip[0], tip[1], z),
                    (tip[0] + bx + px, tip[1] + by + py, z), (tip[0], tip[1], z),
                    (tip[0] + bx - px, tip[1] + by - py, z),
                ]
                arr = []
                for (ax, ay, az) in seg:
                    s = view3d_utils.location_3d_to_region_2d(
                        region, rv3d, mw @ Vector((ax, ay, az)))
                    if s is not None:
                        arr.append((s.x, s.y))
                if len(arr) >= 2:
                    shader.uniform_float("color", _DIRECTION_COLOR)
                    batch_for_shader(shader, 'LINE_STRIP', {"pos": arr}).draw(shader)

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')


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

    _draw_regions(context, region, rv3d, map_props, tiles)

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
