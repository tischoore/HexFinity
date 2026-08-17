"""Flora — click-to-plant tree scattering.

`HEXFINITY_OT_flora_marker` is a modal operator (mirrors `brush.py`'s
cursor-tracking + `regions.py`'s raycast idiom) that raycasts the mouse onto
the map every `MOUSEMOVE`, draws a yellow circle-with-center-dot marker at
the hit point via a `POST_PIXEL` draw handler, and plants a tree on
`LEFTMOUSE`. Esc/RMB exits, same convention as the other HexFinity picking
tools; multiple trees can be planted in one activation.

The rest of the module is the flora subsystem proper — same shape as
`scatter.py`: a shared, session-cached mesh library imported from the STL
assets under `assets/` (one shared `bpy.types.Mesh` per species, linked into
every planted `Object` rather than copied), and `sync_flora`/`purge_flora`,
which `operators.rebuild_tile` calls to re-seat a tile's planted trees onto
its current surface after every rebuild.
"""

import math
import random
from pathlib import Path

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from mathutils import Vector


_MARKER_COLOR = (1.0, 0.9, 0.1, 0.9)
_MARKER_RADIUS_PX = 20.0

# Set/cleared by the modal operator's own lifecycle; read by panel.py so the
# sidebar can show a live indicator while the tool owns viewport input (the
# operator itself can't draw a clickable close button there, since a running
# modal operator swallows clicks before they reach panel buttons).
_ACTIVE = False


def is_active():
    return _ACTIVE


# ---------------------------------------------------------------------------
# Mesh library + caching. Each species is imported from STL once per Blender
# session and cached as a shared mesh datablock (a "linked duplicate" per
# planted tree, not a per-placement copy) — the STLs are several MB each.

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_TREE_TYPE_FOLDERS = {'LEAFY_TREE': "leefytree"}   # one entry per HexFinityFloraProperties.tree_type item
TREE_ASSET_MAN_HEIGHT_MM = 10.0   # the man-height scale the STLs were authored at
FLORA_OF = "hf_flora_of"

_species_cache = {}   # tree_type -> sorted [filenames]
_mesh_cache = {}       # filename -> bpy.types.Mesh (shared, use_fake_user=True)
_mesh_min_z = {}       # filename -> float (lowest local-space vertex Z)


def _list_species(tree_type):
    """Sorted STL filenames for `tree_type`'s asset folder, cached."""
    cached = _species_cache.get(tree_type)
    if cached is not None:
        return cached
    folder = _TREE_TYPE_FOLDERS.get(tree_type)
    files = []
    if folder is not None:
        d = _ASSETS_DIR / folder
        if d.is_dir():
            files = sorted(p.name for p in d.glob("*.stl"))
    _species_cache[tree_type] = files
    return files


def _get_or_import_mesh(tree_type, filename):
    """Return `(mesh, min_z)` for `filename`, importing + caching on first use.

    `min_z` is the lowest local-space vertex Z of the imported mesh, used by
    `sync_flora` to seat the tree's true base (not its bounding-box origin)
    on the surface.
    """
    mesh = _mesh_cache.get(filename)
    if mesh is not None:
        if mesh.name in bpy.data.meshes:
            return mesh, _mesh_min_z[filename]
        # Stale reference (e.g. a different .blend loaded, or a reload) —
        # evict and re-import below.
        del _mesh_cache[filename]
        _mesh_min_z.pop(filename, None)

    folder = _TREE_TYPE_FOLDERS.get(tree_type)
    if folder is None:
        return None, 0.0
    filepath = _ASSETS_DIR / folder / filename
    if not filepath.is_file():
        return None, 0.0

    scene = bpy.context.scene
    before = set(scene.objects)
    try:
        bpy.ops.wm.stl_import(filepath=str(filepath))
    except RuntimeError:
        return None, 0.0
    imported = [o for o in scene.objects if o not in before]
    if not imported:
        return None, 0.0

    mesh = imported[0].data
    mesh.name = f"HF_Flora_{Path(filename).stem}"
    mesh.use_fake_user = True
    min_z = min((v.co.z for v in mesh.vertices), default=0.0)

    bpy.data.objects.remove(imported[0], do_unlink=True)
    for extra in imported[1:]:
        bpy.data.objects.remove(extra, do_unlink=True)

    _mesh_cache[filename] = mesh
    _mesh_min_z[filename] = min_z
    return mesh, min_z


def purge_flora(tile_obj):
    """Remove every planted-tree child of `tile_obj`. Never removes the
    shared cached mesh datablock — it outlives any single object."""
    for child in list(tile_obj.children):
        if child.get(FLORA_OF):
            bpy.data.objects.remove(child, do_unlink=True)


def ensure_flora_collection(context):
    """Get-or-create the map's Flora sub-collection, nested under the map's
    root collection — same get-or-create-once pattern as root_collection
    itself."""
    map_props = context.scene.hexfinity_map
    coll = map_props.flora_collection
    if coll is not None and coll.name in bpy.data.collections:
        return coll
    coll = bpy.data.collections.new("Flora")
    root = map_props.root_collection
    if root is not None:
        root.children.link(coll)
    map_props.flora_collection = coll
    return coll


def sync_flora(tile_obj):
    """(Re)create every planted-tree object for `tile_obj` from its stored
    `flora_placements`, re-seated onto the tile's current (just-rebuilt)
    surface. Assumes the caller already purged any prior objects and ran
    inside `operators._REBUILDING`, with the depsgraph updated so the
    seating raycast below samples the real (current) surface."""
    context = bpy.context
    map_props = context.scene.hexfinity_map
    placements = tile_obj.hexfinity_tile.flora_placements
    if len(placements) == 0:
        return

    coll = ensure_flora_collection(context)
    depsgraph = context.evaluated_depsgraph_get()
    fallback_z = map_props.base_thickness_mm
    down = Vector((0.0, 0.0, -1.0))
    last_z = [fallback_z]

    def surface_z_at(x, y):
        hit, loc, _n, _i = tile_obj.ray_cast(
            Vector((x, y, 1.0e5)), down, depsgraph=depsgraph)
        if hit:
            last_z[0] = loc.z
            return loc.z
        return last_z[0]

    global_scale = map_props.man_height_mm / TREE_ASSET_MAN_HEIGHT_MM
    penetration_mm = context.scene.hexfinity_flora.penetration_mm

    for i, p in enumerate(placements):
        mesh, min_z = _get_or_import_mesh(p.tree_type, p.species_file)
        if mesh is None:
            continue
        surface_z = surface_z_at(p.local_x_mm, p.local_y_mm)
        total_scale = p.scale_factor * global_scale

        obj = bpy.data.objects.new(f"Flora_{tile_obj.name}_{i:03d}", mesh)
        coll.objects.link(obj)
        obj.parent = tile_obj   # default matrix_parent_inverse (identity),
                                 # same as scatter.sync_scatter
        obj.rotation_euler = (0.0, 0.0, p.rotation_rad)
        obj.scale = (total_scale, total_scale, total_scale)
        obj.location = (
            p.local_x_mm,
            p.local_y_mm,
            surface_z - penetration_mm - min_z * total_scale,
        )
        obj[FLORA_OF] = True


class HEXFINITY_OT_flora_marker(bpy.types.Operator):
    bl_idname = "hexfinity.flora_marker"
    bl_label = "Flora"
    bl_description = ("Move the mouse over the map to preview a flora placement "
                      "spot. RMB or Esc closes it.")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.scene.hexfinity_map.is_generated

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Flora must be started in the 3D viewport")
            return {'CANCELLED'}

        self._cursor = (event.mouse_region_x, event.mouse_region_y)
        self._hit_world = None
        self._hit_tile = None

        global _ACTIVE
        _ACTIVE = True

        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw_marker, (context,), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "HexFinity Flora:  move to preview a spot    RMB / Esc = close")
        if context.area is not None:
            context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self._cursor = (event.mouse_region_x, event.mouse_region_y)
            self._update_hit(context, event)
            if context.area is not None:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            self._place_tree(context)
            return {'RUNNING_MODAL'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._finish(context)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def _update_hit(self, context, event):
        region = context.region
        rv3d = context.region_data
        if region is None or region.type != 'WINDOW' or rv3d is None:
            return
        coord = (event.mouse_region_x, event.mouse_region_y)
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)

        depsgraph = context.evaluated_depsgraph_get()
        hit, location, _normal, _index, hit_obj, _matrix = context.scene.ray_cast(
            depsgraph, origin, direction)
        if hit and hit_obj is not None and hit_obj.original.hexfinity_tile.is_generated:
            self._hit_world = location.copy()
            self._hit_tile = hit_obj.original
        else:
            self._hit_world = None
            self._hit_tile = None

    def _place_tree(self, context):
        if self._hit_tile is None or self._hit_world is None:
            return
        tile = self._hit_tile
        flora_props = context.scene.hexfinity_flora

        species_list = _list_species(flora_props.tree_type)
        if not species_list:
            self.report({'WARNING'}, "No tree assets found for this flora type")
            return
        species = random.choice(species_list)

        local = tile.matrix_world.inverted() @ self._hit_world

        placement = tile.hexfinity_tile.flora_placements.add()
        placement.species_file = species
        placement.tree_type = flora_props.tree_type
        placement.local_x_mm = local.x
        placement.local_y_mm = local.y
        placement.rotation_rad = random.uniform(0.0, 2.0 * math.pi)
        pct = flora_props.scale_variation_pct
        placement.scale_factor = 1.0 + random.uniform(-pct, pct) / 100.0

        from .operators import rebuild_tile
        rebuild_tile(tile)
        bpy.ops.ed.undo_push(message="Flora: plant tree")

    def _finish(self, context):
        global _ACTIVE
        _ACTIVE = False
        if self._draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
        context.workspace.status_text_set(None)
        if context.area is not None:
            context.area.tag_redraw()

    def _draw_marker(self, context):
        if self._hit_world is None:
            return
        region = context.region
        rv3d = context.region_data
        if region is None or rv3d is None:
            return
        center = view3d_utils.location_3d_to_region_2d(region, rv3d, self._hit_world)
        if center is None:
            return

        cx, cy = center.x, center.y
        r = _MARKER_RADIUS_PX
        n = 48
        pts = [(cx + r * math.cos(2.0 * math.pi * k / n),
                cy + r * math.sin(2.0 * math.pi * k / n)) for k in range(n)]
        pts.append(pts[0])

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(2.0)
        shader.bind()
        shader.uniform_float("color", _MARKER_COLOR)
        batch_for_shader(shader, 'LINE_STRIP', {"pos": pts}).draw(shader)

        gpu.state.point_size_set(7.0)
        shader.uniform_float("color", _MARKER_COLOR)
        batch_for_shader(shader, 'POINTS', {"pos": [(cx, cy)]}).draw(shader)

        gpu.state.line_width_set(1.0)
        gpu.state.blend_set('NONE')
