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

from . import procedural_surfaces as ps
from .map import DIRECTIONS, neighbour_coord, find_tile
from .mesh_builder import (FLORA_PIN_RADIUS_MM, FLORA_PIN_LENGTH_MM,
                           FLORA_NOTCH_RADIUS_MM, FLORA_NOTCH_DEPTH_MM)


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
FLORA_PIN_OF = "hf_flora_pin_of"   # tags a planted tree's paired pin object
                                   # (parented to the tree, see `sync_flora`)
# Set on BOTH a tree object and its paired pin object at creation time, so
# `notch_specs` and the STL export pairing logic can robustly match them by
# index rather than parsing name strings.
FLORA_PLACEMENT_INDEX = "hf_flora_index"

# Margin applied to a tree's true base-cut radius (see `_get_or_import_mesh`)
# when sizing its flatten pad, so the pad comfortably covers the whole base
# rather than clipping it at the very edge.
PAD_MARGIN = 1.25

_species_cache = {}   # tree_type -> sorted [filenames]
_mesh_cache = {}       # filename -> bpy.types.Mesh (shared, use_fake_user=True)
_mesh_min_z = {}       # filename -> float (lowest local-space vertex Z)
_mesh_footprint_xy = {}   # filename -> (half_x, half_y, local_cx, local_cy),
                          # the local-space XY bbox used by the plant-time
                          # overlap check (see _place_tree / obb_overlap)
_mesh_base_radius = {}   # filename -> float, the true flat-base-cut radius
                         # (see _get_or_import_mesh), used to size flora
                         # pads.pad_specs() without a user-facing slider


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
    """Return `(mesh, min_z, half_x, half_y, local_cx, local_cy, base_radius)`
    for `filename`, importing + caching on first use.

    `min_z` is the lowest local-space vertex Z of the imported mesh, used by
    `sync_flora` to seat the tree's true base (not its bounding-box origin)
    on the surface. `half_x`/`half_y` are the local-space XY bbox
    half-extents and `local_cx`/`local_cy` is that bbox's center offset from
    the mesh origin — together the local-space footprint rectangle used by
    `_place_tree`'s plant-time overlap check (`obb_overlap`). `base_radius` is
    the true flat-base-cut radius (max XY distance from the base verts'
    centroid, among verts within an epsilon of `min_z`) — self-tuning per
    species, used by `pad_specs()` to size each tree's flatten pad.
    """
    mesh = _mesh_cache.get(filename)
    if mesh is not None:
        if mesh.name in bpy.data.meshes:
            return (mesh, _mesh_min_z[filename], *_mesh_footprint_xy[filename],
                    _mesh_base_radius[filename])
        # Stale reference (e.g. a different .blend loaded, or a reload) —
        # evict and re-import below.
        del _mesh_cache[filename]
        _mesh_min_z.pop(filename, None)
        _mesh_footprint_xy.pop(filename, None)
        _mesh_base_radius.pop(filename, None)

    folder = _TREE_TYPE_FOLDERS.get(tree_type)
    if folder is None:
        return None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    filepath = _ASSETS_DIR / folder / filename
    if not filepath.is_file():
        return None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

    scene = bpy.context.scene
    before = set(scene.objects)
    try:
        bpy.ops.wm.stl_import(filepath=str(filepath))
    except RuntimeError:
        return None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    imported = [o for o in scene.objects if o not in before]
    if not imported:
        return None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

    mesh = imported[0].data
    mesh.name = f"HF_Flora_{Path(filename).stem}"
    mesh.use_fake_user = True

    min_x = min_y = min_z = float("inf")
    max_x = max_y = float("-inf")
    max_z = float("-inf")
    for v in mesh.vertices:
        min_x = min(min_x, v.co.x)
        max_x = max(max_x, v.co.x)
        min_y = min(min_y, v.co.y)
        max_y = max(max_y, v.co.y)
        min_z = min(min_z, v.co.z)
        max_z = max(max_z, v.co.z)
    if min_x > max_x:   # no vertices
        min_x = max_x = min_y = max_y = min_z = max_z = 0.0
    half_x = (max_x - min_x) / 2.0
    half_y = (max_y - min_y) / 2.0
    local_cx = (min_x + max_x) / 2.0
    local_cy = (min_y + max_y) / 2.0

    eps = max(1e-4, (max_z - min_z) * 0.002)
    base_pts = [(v.co.x, v.co.y) for v in mesh.vertices if v.co.z <= min_z + eps]
    base_radius = 0.0
    if base_pts:
        bcx = sum(p[0] for p in base_pts) / len(base_pts)
        bcy = sum(p[1] for p in base_pts) / len(base_pts)
        base_radius = max(math.hypot(p[0] - bcx, p[1] - bcy) for p in base_pts)
    if base_radius <= 0.0:
        base_radius = max(half_x, half_y) * 0.15

    bpy.data.objects.remove(imported[0], do_unlink=True)
    for extra in imported[1:]:
        bpy.data.objects.remove(extra, do_unlink=True)

    _mesh_cache[filename] = mesh
    _mesh_min_z[filename] = min_z
    _mesh_footprint_xy[filename] = (half_x, half_y, local_cx, local_cy)
    _mesh_base_radius[filename] = base_radius
    return mesh, min_z, half_x, half_y, local_cx, local_cy, base_radius


def purge_flora(tile_obj):
    """Remove every planted-tree child of `tile_obj`, and each tree's own
    pin child (a pin is parented to its tree, not the tile — see
    `sync_flora`). Never removes the shared cached mesh datablocks — they
    outlive any single object."""
    for child in list(tile_obj.children):
        if child.get(FLORA_OF):
            for pin in list(child.children):
                if pin.get(FLORA_PIN_OF):
                    bpy.data.objects.remove(pin, do_unlink=True)
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


def _surface_sampler(tile_obj, depsgraph, fallback_z):
    """Build a `(x, y) -> current surface z` closure against `tile_obj`'s
    just-rebuilt mesh, used as `sync_flora`'s fallback when no better
    (pre-drilled, hole-free) height is available via `notch_heights`. Falls
    back to the last successful hit (or `fallback_z` before the first one)
    so a raycast miss at the mesh's edge degrades gracefully."""
    down = Vector((0.0, 0.0, -1.0))
    last_z = [fallback_z]

    def surface_z_at(x, y):
        hit, loc, _n, _i = tile_obj.ray_cast(
            Vector((x, y, 1.0e5)), down, depsgraph=depsgraph)
        if hit:
            last_z[0] = loc.z
            return loc.z
        return last_z[0]

    return surface_z_at


def sync_flora(tile_obj, ok_indices=None, notch_heights=None):
    """(Re)create every planted-tree object for `tile_obj` from its stored
    `flora_placements`, re-seated onto the tile's current (just-rebuilt)
    surface. Assumes the caller already purged any prior objects and ran
    inside `operators._REBUILDING`, with the depsgraph updated so the
    seating raycast below samples the real (current) surface.

    `ok_indices`/`notch_heights` (only ever non-empty when finalizing flora
    — see `operators.rebuild_tile`) additionally attach a pin **as a child
    of its tree object** for every placement index in `ok_indices`: pins are
    linked to the tree they belong to, moving as one unit with it, rather
    than sitting as unrelated siblings under the tile. `notch_heights[i]`,
    when present, is the exact pre-drill flat pad height for placement `i`
    (from `tree_pads.cut_notches`) and is used directly instead of a raycast
    — once a socket is cut, raycasting straight down at the placement's
    exact (x, y) would pass through the ~1mm-wide hole and hit the socket
    floor ~`FLORA_NOTCH_DEPTH_MM` below the real surface instead of the
    surrounding pad.
    """
    context = bpy.context
    map_props = context.scene.hexfinity_map
    placements = tile_obj.hexfinity_tile.flora_placements
    if len(placements) == 0:
        return

    coll = ensure_flora_collection(context)
    depsgraph = context.evaluated_depsgraph_get()
    surface_z_at = _surface_sampler(tile_obj, depsgraph, map_props.base_thickness_mm)

    global_scale = map_props.man_height_mm / TREE_ASSET_MAN_HEIGHT_MM
    penetration_mm = context.scene.hexfinity_flora.penetration_mm
    pin_mesh = _get_or_build_pin_mesh() if ok_indices else None

    for i, p in enumerate(placements):
        mesh, min_z, _hx, _hy, _lcx, _lcy, _br = _get_or_import_mesh(p.tree_type, p.species_file)
        if mesh is None:
            continue
        if notch_heights is not None and i in notch_heights:
            surface_z = notch_heights[i]
        else:
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
        obj[FLORA_PLACEMENT_INDEX] = i

        if ok_indices and i in ok_indices:
            pin = bpy.data.objects.new(f"FloraPin_{tile_obj.name}_{i:03d}", pin_mesh)
            coll.objects.link(pin)
            pin.parent = obj   # child of the TREE, not the tile — moves as
                                # one unit with it (default identity
                                # matrix_parent_inverse, same as `obj` above).
            # Counter-scale cancels the tree's own `total_scale` so the pin's
            # real-world size always stays exactly FLORA_PIN_RADIUS_MM /
            # FLORA_PIN_LENGTH_MM, regardless of this tree's random scale.
            inv_scale = 1.0 / total_scale
            pin.scale = (inv_scale, inv_scale, inv_scale)
            # Anchored at `min_z + penetration_mm/total_scale` (not just
            # `min_z`) so the pin's top lands exactly at `surface_z` — the
            # known socket-mouth height — regardless of `penetration_mm`.
            # Without that correction the pin would start `penetration_mm`
            # below the socket's actual mouth, which pokes the pin's tip past
            # the socket floor as soon as `penetration_mm` exceeds
            # `FLORA_PIN_HOLE_TOLERANCE_MM` — a real risk since either value
            # can be changed independently.
            pin.location = (0.0, 0.0, min_z + penetration_mm / total_scale)
            pin[FLORA_PIN_OF] = True
            pin[FLORA_PLACEMENT_INDEX] = i


def pad_specs(tile_obj):
    """Flatten-pad specs for `tile_obj`'s planted trees, one dict per
    placement: `{"x", "y", "radius_mm", "blend_mm"}` in tile-local mm, ready
    for `mesh_builder.build_hex_tile`'s `flora_pads` kwarg.

    Returns `[]` immediately — without importing any species STL — when
    there are no placements or `flatten_base` is off, so a treeless or
    pad-disabled tile pays nothing."""
    flora_props = bpy.context.scene.hexfinity_flora
    if not flora_props.flatten_base:
        return []
    placements = tile_obj.hexfinity_tile.flora_placements
    if len(placements) == 0:
        return []

    map_props = bpy.context.scene.hexfinity_map
    global_scale = map_props.man_height_mm / TREE_ASSET_MAN_HEIGHT_MM
    blend_mm = flora_props.pad_blend_mm

    pads = []
    for p in placements:
        mesh, _min_z, _hx, _hy, _lcx, _lcy, base_radius = _get_or_import_mesh(
            p.tree_type, p.species_file)
        if mesh is None:
            continue
        total_scale = p.scale_factor * global_scale
        pads.append({
            "x": p.local_x_mm,
            "y": p.local_y_mm,
            "radius_mm": base_radius * total_scale * PAD_MARGIN,
            "blend_mm": blend_mm,
        })
    return pads


def notch_specs(tile_obj):
    """Socket-cut specs for `tile_obj`'s planted trees, one dict per
    placement: `{"x", "y", "radius_mm", "depth_mm", "index"}` in tile-local
    mm, ready for `mesh_builder.build_hex_tile`'s `flora_notches` kwarg.

    Unlike `pad_specs`, the socket's size is a fixed hardcoded constant
    (`mesh_builder.FLORA_NOTCH_RADIUS_MM`/`FLORA_NOTCH_DEPTH_MM`) — it must
    stay exactly the same size as the pin object regardless of a given
    tree's random per-placement scale, so no species/scale lookup is needed.

    Gated on `flatten_base` exactly like `pad_specs`: a socket only makes
    sense cut into a surface already known to be flattened flat under the
    tree, so this returns `[]` whenever `pad_specs` would too.
    """
    flora_props = bpy.context.scene.hexfinity_flora
    if not flora_props.flatten_base:
        return []
    placements = tile_obj.hexfinity_tile.flora_placements
    if len(placements) == 0:
        return []

    return [
        {"x": p.local_x_mm, "y": p.local_y_mm,
         "radius_mm": FLORA_NOTCH_RADIUS_MM, "depth_mm": FLORA_NOTCH_DEPTH_MM,
         "index": i}
        for i, p in enumerate(placements)
    ]


# ---------------------------------------------------------------------------
# Pin objects — a small, fixed-size procedural cylinder (never scaled with a
# tree's random size, unlike the species mesh) mating into the socket
# `notch_specs` describes. One shared mesh datablock, same "cached, linked,
# never copied" shape as the species mesh cache above.

_PIN_MESH_NAME = "HF_FloraPin"
_pin_mesh_cache = None
_PIN_SEGMENTS = 12


def _get_or_build_pin_mesh():
    """Return the shared pin `bpy.types.Mesh`, building it once per session.

    A plain vertical cylinder, radius `FLORA_PIN_RADIUS_MM`, height
    `FLORA_PIN_LENGTH_MM`, centred at its own origin with the top face at
    local z=0 (so placing the object at a tree's true base point makes the
    peg point straight down into the tile). Built directly via
    `from_pydata` (no scene-polluting `primitive_cylinder_add` + cleanup
    needed) since the shape is fixed and trivial.
    """
    global _pin_mesh_cache
    if _pin_mesh_cache is not None and _pin_mesh_cache.name in bpy.data.meshes:
        return _pin_mesh_cache

    r = FLORA_PIN_RADIUS_MM
    h = FLORA_PIN_LENGTH_MM
    n = _PIN_SEGMENTS
    verts = []
    for k in range(n):
        a = 2.0 * math.pi * k / n
        verts.append((r * math.cos(a), r * math.sin(a), 0.0))
    for k in range(n):
        a = 2.0 * math.pi * k / n
        verts.append((r * math.cos(a), r * math.sin(a), -h))
    top_center = len(verts)
    verts.append((0.0, 0.0, 0.0))
    bottom_center = len(verts)
    verts.append((0.0, 0.0, -h))

    faces = []
    for k in range(n):
        k1 = (k + 1) % n
        faces.append((top_center, k, k1))                        # top cap, +Z
        faces.append((bottom_center, n + k1, n + k))              # bottom cap, -Z
        faces.append((k, n + k, n + k1, k1))                      # wall, outward

    mesh = bpy.data.meshes.new(_PIN_MESH_NAME)
    mesh.from_pydata(verts, [], faces)
    mesh.update(calc_edges=True)
    mesh.use_fake_user = True
    _pin_mesh_cache = mesh
    return mesh


# ---------------------------------------------------------------------------
# Plant-time overlap check. Each tree's footprint is an oriented rectangle
# (its local-space XY bbox, rotated by its own random Z spin) tested against
# every other planted tree via procedural_surfaces.obb_overlap — see
# _place_tree below.

def _placement_footprint(tile_obj, placement, global_scale):
    """World-space OBB `(cx, cy, hx, hy, angle)` for one stored placement.

    Tile objects are placed by translation only (see operators._build_map /
    _create_tile — never rotated), so the placement's own `rotation_rad` is
    already the world-space angle; only the position needs the tile's
    matrix_world applied.
    """
    _mesh, _min_z, half_x, half_y, local_cx, local_cy, _br = _get_or_import_mesh(
        placement.tree_type, placement.species_file)
    scale = placement.scale_factor * global_scale
    angle = placement.rotation_rad
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    # The bbox's local-space center offset rotates + scales with the tree;
    # add it to the placement's stored (already tile-local) plant point.
    off_x = (local_cx * cos_a - local_cy * sin_a) * scale
    off_y = (local_cx * sin_a + local_cy * cos_a) * scale
    local = Vector((placement.local_x_mm + off_x, placement.local_y_mm + off_y, 0.0))
    world = tile_obj.matrix_world @ local
    return (world.x, world.y, half_x * scale, half_y * scale, angle)


def _nearby_placements(context, tile):
    """Yield `(tile_obj, placement)` for every stored flora placement on
    `tile` and each of its 6 grid neighbours — a tree near a tile seam must
    not overlap into a neighbouring tile's print, so the check has to look
    past the current tile's own placements."""
    scene = context.scene
    tprops = tile.hexfinity_tile
    tiles = [tile]
    for direction in DIRECTIONS:
        nq, nr = neighbour_coord(tprops.coord_q, tprops.coord_r, direction)
        neighbour = find_tile(scene, nq, nr)
        if neighbour is not None:
            tiles.append(neighbour)
    for t in tiles:
        for p in t.hexfinity_tile.flora_placements:
            yield t, p


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
        # (q, r) coords touched this activation, not object references — an
        # undo_push fires after every placement, so a cached Object could go
        # stale mid-session; resolved back to a live tile in _finish().
        self._touched_coords = set()

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

        _mesh, _min_z, half_x, half_y, local_cx, local_cy, _br = _get_or_import_mesh(
            flora_props.tree_type, species)

        local = tile.matrix_world.inverted() @ self._hit_world
        rotation_rad = random.uniform(0.0, 2.0 * math.pi)
        pct = flora_props.scale_variation_pct
        scale_factor = 1.0 + random.uniform(-pct, pct) / 100.0

        if flora_props.avoid_overlap:
            map_props = context.scene.hexfinity_map
            global_scale = map_props.man_height_mm / TREE_ASSET_MAN_HEIGHT_MM
            scale = scale_factor * global_scale
            cos_a, sin_a = math.cos(rotation_rad), math.sin(rotation_rad)
            off_x = (local_cx * cos_a - local_cy * sin_a) * scale
            off_y = (local_cx * sin_a + local_cy * cos_a) * scale
            cand_world = self._hit_world
            cand = (cand_world.x + off_x, cand_world.y + off_y,
                    half_x * scale, half_y * scale, rotation_rad)
            min_gap = context.scene.hexfinity_flora.min_spacing_mm
            for other_tile, other_placement in _nearby_placements(context, tile):
                other = _placement_footprint(other_tile, other_placement, global_scale)
                if ps.obb_overlap(cand[0], cand[1], cand[2], cand[3], cand[4],
                                   other[0], other[1], other[2], other[3], other[4],
                                   min_gap=min_gap):
                    self.report({'WARNING'}, "Too close to another tree — move further away")
                    return

        placement = tile.hexfinity_tile.flora_placements.add()
        placement.species_file = species
        placement.tree_type = flora_props.tree_type
        placement.local_x_mm = local.x
        placement.local_y_mm = local.y
        placement.rotation_rad = rotation_rad
        placement.scale_factor = scale_factor

        from .operators import rebuild_tile
        rebuild_tile(tile)
        tp = tile.hexfinity_tile
        self._touched_coords.add((tp.coord_q, tp.coord_r))
        bpy.ops.ed.undo_push(message="Flora: plant tree")

    def _finish(self, context):
        global _ACTIVE
        _ACTIVE = False
        if self._draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
        context.workspace.status_text_set(None)

        # Cut pin/notch sockets now, once, for every tile touched this
        # activation — deferred from the per-click rebuilds above so rapid
        # placement/preview stays fast (see rebuild_tile's finalize_flora
        # docstring).
        if self._touched_coords:
            from .operators import rebuild_tile
            finalized = False
            for q, r in self._touched_coords:
                tile = find_tile(context.scene, q, r)
                if tile is not None:
                    rebuild_tile(tile, finalize_flora=True)
                    finalized = True
            if finalized:
                bpy.ops.ed.undo_push(message="Flora: finalize pins/notches")
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


class HEXFINITY_OT_finalize_flora(bpy.types.Operator):
    bl_idname = "hexfinity.finalize_flora"
    bl_label = "Finalize Flora"
    bl_description = ("Cut pin/notch sockets for every planted tree on the "
                      "map. Pins/notches only exist right after this runs — "
                      "any later edit (brush, corner height, terrain snap, "
                      "or a flora pad setting change) strips them again "
                      "until it's run once more")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.hexfinity_map.is_generated

    def execute(self, context):
        map_props = context.scene.hexfinity_map
        coll = map_props.root_collection
        if coll is None:
            self.report({'WARNING'}, "No map generated")
            return {'CANCELLED'}

        from .operators import rebuild_tile
        count = 0
        for obj in coll.objects:
            tile_props = obj.hexfinity_tile
            if tile_props.is_generated and len(tile_props.flora_placements) > 0:
                rebuild_tile(obj, finalize_flora=True)
                count += 1

        if count == 0:
            self.report({'WARNING'}, "No planted trees found")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Finalized flora on {count} tile(s)")
        return {'FINISHED'}
