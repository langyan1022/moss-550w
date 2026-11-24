"""在临时 Web 场景中合并同一刚体上的不透明静态网格。"""

from collections import defaultdict
import hashlib
import json
from time import perf_counter

import bpy
from exterior_gltf import write_exterior_gltf


def batch_key(obj):
    if obj.type != "MESH" or not obj.parent or len(obj.material_slots) != 1:
        return None
    if str(obj.get("sourceName","")).startswith("P10_主体正盖面板01"):
        return None  # 前端按这个零件的 CAD 平面处理 H50 的图集采样。
    material = obj.material_slots[0].material
    if not material or not material.node_tree or obj.matrix_world.to_3x3().determinant() <= 0:
        return None
    shader = next((n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if not shader:
        return None
    alpha, transmission = shader.inputs["Alpha"], shader.inputs["Transmission Weight"]
    if alpha.is_linked or alpha.default_value < 1 or transmission.is_linked or transmission.default_value > 0:
        return None
    mesh = obj.data
    uv = tuple((u.name, u.active_render, u.active) for u in mesh.uv_layers)
    colors = tuple((a.name, a.domain, a.data_type) for a in mesh.color_attributes)
    return obj.parent, material, uv, colors


def face_count(obj):
    # 每个 n 边形对应 n−2 个三角面。
    return len(obj.data.loops) - 2 * len(obj.data.polygons)


def export_batched(web, output, info):
    start = perf_counter()
    if bpy.context.scene != web:
        raise ValueError("合批需要独立的 Web 导出场景")
    originals = [obj for obj in web.objects if obj.type == "MESH"]
    by_material = defaultdict(list)
    for obj in originals:
        if key := batch_key(obj):
            by_material[key].append(obj)
    groups = {}
    for (parent, material, _, _), objects in by_material.items():
        if len(objects) < 2:
            continue
        name = f"{parent.name}-batch-{len(groups):03d}"
        objects.sort(key=lambda o:o.name)
        before = sum(face_count(o) for o in objects)
        sources = [{"node":o.name, **{k:o[k] for k in o.keys() if k != "_RNA_UI"}} for o in objects]
        active = objects[0]
        with bpy.context.temp_override(active_object=active, object=active,
                                       selected_editable_objects=objects, selected_objects=objects):
            if "FINISHED" not in bpy.ops.object.join():
                raise RuntimeError("机身分组合并失败：" + name)
        if active.parent != parent or len(active.material_slots) != 1 or face_count(active) != before:
            raise ValueError("合批使材质、关节或三角面数变化：" + name)
        for key in list(active.keys()):
            del active[key]
        active.name = name
        active["sourceBatch"] = name
        groups[name] = {"group":parent.name,"material":material.name,"members":sources}
    count = sum(o.type == "MESH" for o in web.objects)
    if not groups or count >= len(originals):
        raise ValueError("本次导出没有合并可绘制对象")
    stage = output.with_name(output.stem+".building.glb")
    manifest = output.with_suffix(".json")
    temp = manifest.with_suffix(".building.json")
    try:
        write_exterior_gltf(stage)
        with stage.open("rb") as stream:
            digest = hashlib.file_digest(stream,"sha256").hexdigest()
        batch_info = {"beforeObjects":len(originals),"afterObjects":count,"bysource":groups,
                      "seconds":round(perf_counter()-start,2)}
        result = {**info,"bytes":stage.stat().st_size,"glbSha256":digest,"batching":batch_info}
        temp.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        stage.replace(output)
        temp.replace(manifest)
        print("合批完成", output, len(originals), count, batch_info["seconds"], flush=True)
    finally:
        stage.unlink(missing_ok=True)
        temp.unlink(missing_ok=True)
