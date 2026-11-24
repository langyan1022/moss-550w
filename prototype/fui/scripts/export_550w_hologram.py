"""
550W MOSS Real CAD Holographic Wireframe & Depth-Shell Exporter (Blender 5.2+)

Reads the official read-only 550W CAD assets (`moss-exterior.glb` + `moss-tof.glb`),
seats the dual-ring TOF optical assembly into the exact faceplate socket, extracts
clean architectural CAD feature edges via weld + Limited Dissolve + dihedral angle
filtering, generates a lightweight depth-occlusion Fresnel shell, and exports:
  1. `public/vendor/models/550w-hologram.glb`  — lightweight real glTF 2.0 hologram model
  2. `public/vendor/models/550w-hologram.json` — compact WebGL2/Canvas2D runtime CAD asset
  3. Optional multi-angle Blender verification contact sheet (--preview <png>)

Usage:
  blender -b --factory-startup --python-exit-code 1 -P scripts/export_550w_hologram.py -- \
    --assets "F:/moss/agent-lab/prototype/static/conversation-assets" \
    --out-dir "F:/moss-fui/public/vendor/models"
"""

import argparse
import json
import math
import os
import sys
import bpy
import bmesh
from mathutils import Matrix, Vector


# Exact faceplate optical basis from moss-render.js (glTF Y-up) converted to Blender Z-up
# glTF (x, y, z) -> Blender (x, -z, y)
def gltf_to_blender_vec(v):
    return Vector((v[0], -v[2], v[1]))


def blender_to_gltf_vec(v):
    return Vector((v[0], v[2], -v[1]))


# Faceplate orthonormal frame in glTF Y-up
_S_GLTF = Vector((0.998449769778, 0.0, 0.055660194313))
_F_GLTF = Vector((0.003517443531, 0.998001201169, -0.063096989273))
_A_GLTF = Vector((-0.055548940782, 0.063194956004, 0.996454069545))

# Optical insert center in glTF Y-up: 48.744*S + 96.374*F + 24.05*A
_TOF_CENTER_GLTF = _S_GLTF * 48.744 + _F_GLTF * 96.374 + _A_GLTF * 24.05
TOF_CENTER_BLENDER = gltf_to_blender_vec(_TOF_CENTER_GLTF)

# Rotation matrix mapping moss-tof.glb local frame (X=right, -Y=front, Z=up) into Blender head socket
_BX = gltf_to_blender_vec(_S_GLTF).normalized()
_BY = (-gltf_to_blender_vec(_A_GLTF)).normalized()
_BZ = gltf_to_blender_vec(_F_GLTF).normalized()
TOF_ROT_BLENDER = Matrix((
    (_BX.x, _BY.x, _BZ.x, 0.0),
    (_BX.y, _BY.y, _BZ.y, 0.0),
    (_BX.z, _BY.z, _BZ.z, 0.0),
    (0.0,   0.0,   0.0,   1.0),
))
TOF_XFORM_BLENDER = Matrix.Translation(TOF_CENTER_BLENDER) @ TOF_ROT_BLENDER


SKIP_NAME_TOKENS = (
    "独立制作标识",
    "肘部 CORE MODULE",
    "肘部 M-01",
    "侧盖浅色识别印记",
    "head-batch-066",   # 90k-tri microscopic strip decal
    "CAD-零件 1174",     # 83k-tri thin washer ring
)


def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser()
    p.add_argument("--assets", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--preview", default="")
    return p.parse_args(argv)


def world_bbox_diag(ob):
    mat = ob.matrix_world
    pts = [mat @ Vector(c) for c in ob.bound_box]
    bmin = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    bmax = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return (bmax - bmin).length, bmin, bmax


def should_skip_mesh(ob, min_diag):
    name = ob.name
    for tok in SKIP_NAME_TOKENS:
        if tok in name:
            return True
    diag, _, _ = world_bbox_diag(ob)
    if diag < min_diag:
        return True
    return False


def collect_meshes_into_bmesh(objects, extra_xform=None):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    bm = bmesh.new()
    used = 0
    for ob in objects:
        if ob.type != 'MESH':
            continue
        ev = ob.evaluated_get(depsgraph)
        me = ev.to_mesh()
        if len(me.vertices) == 0 or len(me.polygons) == 0:
            ev.to_mesh_clear()
            continue
        tmp = bmesh.new()
        tmp.from_mesh(me)
        mat = ob.matrix_world.copy()
        if extra_xform is not None:
            mat = extra_xform @ mat
        bmesh.ops.transform(tmp, matrix=mat, verts=tmp.verts)
        # Copy into master bmesh via temporary mesh merge (fast in C)
        me_tmp = bpy.data.meshes.new("_tmp_copy")
        tmp.to_mesh(me_tmp)
        tmp.free()
        bm.from_mesh(me_tmp)
        bpy.data.meshes.remove(me_tmp)
        ev.to_mesh_clear()
        used += 1
    return bm, used


def extract_cad_edges(
    bm,
    weld_dist=0.55,
    dissolve_deg=14.0,
    sharp_deg=33.0,
    min_edge_len=1.8,
    dedupe_grid=0.85,
    max_edges=2400,
):
    bm_work = bm.copy()
    bmesh.ops.remove_doubles(bm_work, verts=bm_work.verts, dist=weld_dist)
    bmesh.ops.dissolve_limit(
        bm_work,
        angle_limit=math.radians(dissolve_deg),
        use_dissolve_boundaries=False,
        verts=bm_work.verts,
        edges=bm_work.edges,
        delimit=set(),
    )
    bm_work.faces.ensure_lookup_table()
    bm_work.edges.ensure_lookup_table()
    for f in bm_work.faces:
        f.normal_update()

    sharp_rad = math.radians(sharp_deg)
    seen = set()
    candidates = []

    def qkey(v):
        return (
            int(round(v.x / dedupe_grid)),
            int(round(v.y / dedupe_grid)),
            int(round(v.z / dedupe_grid)),
        )

    for e in bm_work.edges:
        v0 = e.verts[0].co
        v1 = e.verts[1].co
        length = (v1 - v0).length
        if length < min_edge_len:
            continue

        lf = e.link_faces
        keep = False
        angle_score = 1.0
        if len(lf) == 1:
            # Keep clean panel perimeter boundaries, skip tiny interior stray slits
            if length >= min_edge_len * 1.4:
                keep = True
                angle_score = 0.85
        elif len(lf) == 2:
            n0 = lf[0].normal
            n1 = lf[1].normal
            if n0.length > 1e-6 and n1.length > 1e-6:
                dot = max(-1.0, min(1.0, n0.dot(n1)))
                ang = math.acos(dot)
                if ang >= sharp_rad:
                    keep = True
                    # Favor true 60-90 deg machined CAD edges over shallow chamfers
                    angle_score = 0.5 + (ang / math.pi) * 1.4
        elif len(lf) > 2:
            keep = True
            angle_score = 1.1

        if not keep:
            continue

        k0 = qkey(v0)
        k1 = qkey(v1)
        if k0 == k1:
            continue
        ek = (k0, k1) if k0 < k1 else (k1, k0)
        if ek in seen:
            continue
        seen.add(ek)

        # Balance edge length & sharpness so short curved arcs around cylinders/bezels survive too
        score = math.sqrt(length) * angle_score
        candidates.append((score, v0.copy(), v1.copy()))

    bm_work.free()

    candidates.sort(key=lambda item: item[0], reverse=True)
    if len(candidates) > max_edges:
        candidates = candidates[:max_edges]

    return [(a, b) for _, a, b in candidates]


def build_decimated_shell(bm_all, target_tris=3200):
    me = bpy.data.meshes.new("MOSS_550W_HoloShell")
    bm_all.to_mesh(me)
    ob = bpy.data.objects.new("MOSS_550W_HoloShell", me)
    bpy.context.collection.objects.link(ob)

    # Voxel remesh fuses all CAD sub-parts into ONE clean watertight envelope for Fresnel & depth occlusion
    mod_r = ob.modifiers.new("Remesh", 'REMESH')
    mod_r.mode = 'VOXEL'
    mod_r.voxel_size = 2.6
    mod_r.adaptivity = 0.35
    mod_r.use_smooth_shade = True

    depsgraph = bpy.context.evaluated_depsgraph_get()
    ev0 = ob.evaluated_get(depsgraph)
    m0 = ev0.to_mesh()
    m0.calc_loop_triangles()
    cur_tris = max(1, len(m0.loop_triangles))
    ev0.to_mesh_clear()

    ratio = min(1.0, max(0.01, float(target_tris) / float(cur_tris)))
    mod_d = ob.modifiers.new("Decimate", 'DECIMATE')
    mod_d.decimate_type = 'COLLAPSE'
    mod_d.ratio = ratio
    mod_d.use_collapse_triangulate = True

    depsgraph = bpy.context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(depsgraph)
    me_dec = ev.to_mesh()
    me_dec.calc_loop_triangles()

    verts = [v.co.copy() for v in me_dec.vertices]
    tris = []
    for lt in me_dec.loop_triangles:
        tris.append((lt.vertices[0], lt.vertices[1], lt.vertices[2]))

    ev.to_mesh_clear()
    bpy.data.objects.remove(ob, do_unlink=True)
    bpy.data.meshes.remove(me)
    return verts, tris


def main():
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    ext_path = os.path.join(args.assets, "moss-exterior.glb")
    tof_path = os.path.join(args.assets, "moss-tof.glb")
    if not os.path.isfile(ext_path) or not os.path.isfile(tof_path):
        raise FileNotFoundError(f"Missing 550W GLB assets in {args.assets}")

    # 1. Import Exterior CAD Model
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=ext_path)

    groups_objs = {"mount": [], "shoulder": [], "elbow": [], "head": []}
    for ob in bpy.data.objects:
        if ob.type != 'MESH':
            continue
        pname = ob.parent.name if ob.parent else ""
        if pname not in groups_objs:
            continue
        # Keep structural parts (filter microscopic decals/fasteners)
        min_diag = 6.0 if pname == "head" else 5.0
        if should_skip_mesh(ob, min_diag=min_diag):
            continue
        groups_objs[pname].append(ob)

    group_bms = {}
    master_shell_bm = bmesh.new()
    part_stats = {}

    for gname, objs in groups_objs.items():
        bm, used = collect_meshes_into_bmesh(objs)
        group_bms[gname] = bm
        # Append to shell bmesh
        me_t = bpy.data.meshes.new("_tmp_shell")
        bm.to_mesh(me_t)
        master_shell_bm.from_mesh(me_t)
        bpy.data.meshes.remove(me_t)
        part_stats[gname] = {"meshes": used, "raw_verts": len(bm.verts), "raw_faces": len(bm.faces)}

    # 2. Import TOF Optical Lens Assembly & seat into exact faceplate socket
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=tof_path)
    tof_objs = [
        ob for ob in bpy.data.objects
        if ob.type == 'MESH' and not should_skip_mesh(ob, min_diag=2.2)
    ]
    bm_tof, tof_used = collect_meshes_into_bmesh(tof_objs, extra_xform=TOF_XFORM_BLENDER)
    part_stats["tof"] = {"meshes": tof_used, "raw_verts": len(bm_tof.verts), "raw_faces": len(bm_tof.faces)}

    # Also add TOF to shell bmesh so lens cavity is properly depth-occluded
    me_t = bpy.data.meshes.new("_tmp_tof_shell")
    bm_tof.to_mesh(me_t)
    master_shell_bm.from_mesh(me_t)
    bpy.data.meshes.remove(me_t)

    # 3. Extract Clean Architectural CAD Feature Edges per group
    edge_budgets = {
        "head":     dict(weld_dist=0.55, dissolve_deg=14.0, sharp_deg=33.0, min_edge_len=1.8, dedupe_grid=0.85, max_edges=2400),
        "elbow":    dict(weld_dist=0.55, dissolve_deg=14.0, sharp_deg=33.0, min_edge_len=1.8, dedupe_grid=0.85, max_edges=720),
        "shoulder": dict(weld_dist=0.55, dissolve_deg=14.0, sharp_deg=33.0, min_edge_len=1.8, dedupe_grid=0.85, max_edges=820),
        "mount":    dict(weld_dist=0.55, dissolve_deg=14.0, sharp_deg=33.0, min_edge_len=1.8, dedupe_grid=0.85, max_edges=560),
    }

    extracted_groups = {}
    all_pts = []
    for gname, cfg in edge_budgets.items():
        edges = extract_cad_edges(group_bms[gname], **cfg)
        extracted_groups[gname] = edges
        part_stats[gname]["edges"] = len(edges)
        for a, b in edges:
            all_pts.append(a)
            all_pts.append(b)
        group_bms[gname].free()

    tof_edges = extract_cad_edges(
        bm_tof,
        weld_dist=0.18,
        dissolve_deg=14.0,
        sharp_deg=32.0,
        min_edge_len=0.75,
        dedupe_grid=0.35,
        max_edges=460,
    )
    extracted_groups["tof"] = tof_edges
    part_stats["tof"]["edges"] = len(tof_edges)
    for a, b in tof_edges:
        all_pts.append(a)
        all_pts.append(b)
    bm_tof.free()

    # 4. Build Decimated Hologram Depth Shell
    bpy.ops.wm.read_factory_settings(use_empty=True)
    shell_verts_raw, shell_tris = build_decimated_shell(master_shell_bm, target_tris=3600)
    master_shell_bm.free()

    # 5. Compute Unified Normalization Transform (centered, max dimension = 2.0, WebGL Y-up)
    bmin = Vector((
        min(p.x for p in all_pts),
        min(p.y for p in all_pts),
        min(p.z for p in all_pts),
    ))
    bmax = Vector((
        max(p.x for p in all_pts),
        max(p.y for p in all_pts),
        max(p.z for p in all_pts),
    ))
    center_bl = (bmin + bmax) * 0.5
    dims_bl = bmax - bmin
    max_dim = max(dims_bl.x, dims_bl.y, dims_bl.z, 1e-6)
    scale = 2.0 / max_dim

    def norm_gltf(v_bl):
        g = blender_to_gltf_vec((v_bl - center_bl) * scale)
        return [round(g.x, 4), round(g.y, 4), round(g.z, 4)]

    # Quantized integer coordinates (x10000 -> ~0.0001 precision, ultra-compact JSON)
    QUANT = 10000

    def norm_gltf_q(v_bl):
        g = blender_to_gltf_vec((v_bl - center_bl) * scale)
        return (
            int(round(g.x * QUANT)),
            int(round(g.y * QUANT)),
            int(round(g.z * QUANT)),
        )

    # Pack exterior edges and TOF edges into shared quantized vertex tables
    def pack_edges(edge_list):
        vmap = {}
        verts = []
        indices = []
        for a, b in edge_list:
            qa = norm_gltf_q(a)
            qb = norm_gltf_q(b)
            if qa == qb:
                continue
            ia = vmap.get(qa)
            if ia is None:
                ia = len(verts) // 3
                vmap[qa] = ia
                verts.extend(qa)
            ib = vmap.get(qb)
            if ib is None:
                ib = len(verts) // 3
                vmap[qb] = ib
                verts.extend(qb)
            indices.append(ia)
            indices.append(ib)
        return verts, indices

    ext_edges_all = (
        extracted_groups["head"]
        + extracted_groups["elbow"]
        + extracted_groups["shoulder"]
        + extracted_groups["mount"]
    )
    ext_v, ext_i = pack_edges(ext_edges_all)
    tof_v, tof_i = pack_edges(extracted_groups["tof"])

    # Pack decimated depth shell (quantized positions + triangle indices)
    shell_v = []
    for v in shell_verts_raw:
        shell_v.extend(norm_gltf_q(v))
    shell_i = []
    for a, b, c in shell_tris:
        shell_i.extend((a, b, c))

    # Normalized TOF center & optical forward vector in glTF Y-up
    tof_center_norm = norm_gltf(TOF_CENTER_BLENDER)
    tof_fwd_norm = [round(_A_GLTF.x, 4), round(_A_GLTF.y, 4), round(_A_GLTF.z, 4)]
    tof_radius_norm = round(8.46 * scale, 4)

    # Group bounding centers (useful for camera preset focus)
    head_edges = extracted_groups["head"]
    head_pts = [p for ab in head_edges for p in ab]
    head_center_bl = Vector((
        sum(p.x for p in head_pts) / len(head_pts),
        sum(p.y for p in head_pts) / len(head_pts),
        sum(p.z for p in head_pts) / len(head_pts),
    ))
    head_center_norm = norm_gltf(head_center_bl)

    json_payload = {
        "version": 1,
        "source": "550W MOSS CAD (moss-exterior.glb + moss-tof.glb)",
        "generator": f"Blender {bpy.app.version_string}",
        "quant": QUANT,
        "bounds": {
            "raw_dims_mm": [round(dims_bl.x, 2), round(dims_bl.y, 2), round(dims_bl.z, 2)],
            "norm_dims": [round(dims_bl.x * scale, 4), round(dims_bl.z * scale, 4), round(dims_bl.y * scale, 4)],
        },
        "anchors": {
            "tofCenter": tof_center_norm,
            "tofForward": tof_fwd_norm,
            "tofRadius": tof_radius_norm,
            "headCenter": head_center_norm,
        },
        "exterior": {
            "verts": ext_v,
            "edges": ext_i,
        },
        "tof": {
            "verts": tof_v,
            "edges": tof_i,
        },
        "shell": {
            "verts": shell_v,
            "tris": shell_i,
        },
    }

    json_path = os.path.join(args.out_dir, "550w-hologram.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(json_payload, fh, separators=(",", ":"))

    # 6. Build Clean Blender Scene & Export Real 550W Hologram .glb
    bpy.ops.wm.read_factory_settings(use_empty=True)
    col = bpy.context.collection

    def make_holo_mat(name, color_rgba, emission_strength=2.2, alpha=1.0):
        mat = bpy.data.materials.new(name)
        nt = mat.node_tree
        bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf:
            bsdf.inputs["Base Color"].default_value = color_rgba
            bsdf.inputs["Roughness"].default_value = 0.4
            bsdf.inputs["Metallic"].default_value = 0.0
            if "Emission Color" in bsdf.inputs:
                bsdf.inputs["Emission Color"].default_value = color_rgba
                bsdf.inputs["Emission Strength"].default_value = emission_strength
            if "Alpha" in bsdf.inputs:
                bsdf.inputs["Alpha"].default_value = alpha
        if alpha < 0.99:
            mat.blend_method = 'BLEND' if hasattr(mat, 'blend_method') else 'HASHED'
        return mat

    mat_shell = make_holo_mat("M_550W_HoloShell", (0.04, 0.28, 0.38, 1.0), emission_strength=0.35, alpha=0.18)
    mat_wire = make_holo_mat("M_550W_HoloWire", (0.31, 0.82, 0.90, 1.0), emission_strength=3.0, alpha=1.0)
    mat_tof = make_holo_mat("M_550W_HoloTOF", (1.0, 0.30, 0.25, 1.0), emission_strength=4.0, alpha=1.0)

    # Shell object (in normalized Blender Z-up so glTF export writes exact normalized Y-up!)
    def norm_bl(v_bl):
        return (v_bl - center_bl) * scale

    me_shell_out = bpy.data.meshes.new("SM_550W_HoloShell")
    me_shell_out.from_pydata([norm_bl(v) for v in shell_verts_raw], [], shell_tris)
    me_shell_out.update()
    me_shell_out.materials.append(mat_shell)
    ob_shell = bpy.data.objects.new("SM_550W_HoloShell", me_shell_out)
    col.objects.link(ob_shell)

    # Exterior CAD edge mesh (pure line primitives — valid glTF LINES mode!)
    def make_edge_mesh(name, edge_list, mat):
        vmap = {}
        verts = []
        edges = []
        for a, b in edge_list:
            na = norm_bl(a)
            nb = norm_bl(b)
            ka = (round(na.x, 4), round(na.y, 4), round(na.z, 4))
            kb = (round(nb.x, 4), round(nb.y, 4), round(nb.z, 4))
            if ka == kb:
                continue
            ia = vmap.get(ka)
            if ia is None:
                ia = len(verts)
                vmap[ka] = ia
                verts.append(na)
            ib = vmap.get(kb)
            if ib is None:
                ib = len(verts)
                vmap[kb] = ib
                verts.append(nb)
            edges.append((ia, ib))
        me = bpy.data.meshes.new(name)
        me.from_pydata(verts, edges, [])
        me.update()
        me.materials.append(mat)
        ob = bpy.data.objects.new(name, me)
        col.objects.link(ob)
        return ob

    ob_ext_wire = make_edge_mesh("SM_550W_Wireframe_Exterior", ext_edges_all, mat_wire)
    ob_tof_wire = make_edge_mesh("SM_550W_Wireframe_TOF", extracted_groups["tof"], mat_tof)

    glb_path = os.path.join(args.out_dir, "550w-hologram.glb")
    bpy.ops.export_scene.gltf(
        filepath=glb_path,
        export_format='GLB',
        use_selection=False,
        export_yup=True,
        export_materials='EXPORT',
        export_normals=True,
    )

    # 7. Optional Multi-Angle Preview Render via EEVEE/Workbench for QA inspection
    preview_written = ""
    if args.preview:
        scene = bpy.context.scene
        scene.render.engine = 'BLENDER_WORKBENCH'
        scene.render.resolution_x = 960
        scene.render.resolution_y = 540
        scene.render.film_transparent = False
        scene.display.shading.light = 'FLAT'
        scene.display.shading.color_type = 'OBJECT'
        scene.display.shading.show_object_outline = False
        scene.world = bpy.data.worlds.new("HoloWorld")
        scene.world.color = (0.005, 0.02, 0.03)

        # Show thin tubular wireframe preview for Workbench visibility (convert loose edges via SKIN/CURVE? Use Grease Pencil or Wireframe + dark shell)
        ob_shell.color = (0.04, 0.22, 0.30, 1.0)
        # Convert edge meshes to thin curves ONLY for the preview render
        def edges_to_preview_curve(ob_src, radius, rgba):
            cu = bpy.data.curves.new(ob_src.name + "_cu", 'CURVE')
            cu.dimensions = '3D'
            cu.bevel_depth = radius
            cu.bevel_resolution = 0
            me = ob_src.data
            verts = me.vertices
            for e in me.edges:
                sp = cu.splines.new('POLY')
                sp.points.add(1)
                p0 = verts[e.vertices[0]].co
                p1 = verts[e.vertices[1]].co
                sp.points[0].co = (p0.x, p0.y, p0.z, 1.0)
                sp.points[1].co = (p1.x, p1.y, p1.z, 1.0)
            ob_c = bpy.data.objects.new(ob_src.name + "_preview", cu)
            ob_c.color = rgba
            col.objects.link(ob_c)
            return ob_c

        edges_to_preview_curve(ob_ext_wire, 0.0038, (0.31, 0.85, 0.94, 1.0))
        edges_to_preview_curve(ob_tof_wire, 0.0044, (1.0, 0.32, 0.26, 1.0))
        ob_ext_wire.hide_render = True
        ob_tof_wire.hide_render = True

        cam_data = bpy.data.cameras.new("PreviewCam")
        cam_data.lens = 50.0
        cam_ob = bpy.data.objects.new("PreviewCam", cam_data)
        col.objects.link(cam_ob)
        scene.camera = cam_ob

        # Position at classic 3/4 hero angle facing -Y (front of 550W, full silhouette visible)
        cam_pos = Vector((-1.65, -4.35, 0.22))
        target = Vector((0.0, 0.0, 0.0))
        direction = target - cam_pos
        cam_ob.location = cam_pos
        cam_ob.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()

        scene.render.filepath = args.preview
        bpy.ops.render.render(write_still=True)
        preview_written = args.preview

    report = {
        "ok": True,
        "blender": bpy.app.version_string,
        "parts": part_stats,
        "exterior_edges": len(ext_i) // 2,
        "tof_edges": len(tof_i) // 2,
        "shell_verts": len(shell_v) // 3,
        "shell_tris": len(shell_i) // 3,
        "bounds": json_payload["bounds"],
        "anchors": json_payload["anchors"],
        "files": {
            "json": json_path,
            "json_bytes": os.path.getsize(json_path),
            "glb": glb_path,
            "glb_bytes": os.path.getsize(glb_path),
            "preview": preview_written,
        },
    }
    print("@@JSON@@" + json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
