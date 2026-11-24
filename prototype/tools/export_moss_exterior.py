import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from exterior_wear import first_layer, make_wear, read_recipes
from exterior_pbr import link_pbr
from exterior_joint_normals import restore_joint_normals
from exterior_gltf import EXPONENTIAL_BITS, write_exterior_gltf


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def chain(item):
    while item:
        yield item
        item = item.parent


def exterior_group(obj):
    names = list(chain(obj))
    rigs = [o.get("mossRig") for o in names if o.get("mossRig")]
    if not rigs or "railTravel" not in rigs:
        return None
    if obj.get("mossGroup") in {"rail", "carriage", "mount-clip"}:
        return None
    if (any(o.get("mossTofRole") in {"front-outer-ring","front-inner-ring","fixed-optical-center"} for o in names)
            or obj.get("mossAnimatedDisplay") or obj.name.startswith("Clear domed LED encapsulation")):
        return None
    # 齿轮防护罩随主体固定。其下的驱动件、卡座、内部电路均在闭合外壳以内。
    guard = str(obj.get("mossTofRole","")).startswith("tof-service-gear-guard")
    internal = ("mossRefinedInteriorRole","mossInternalSupport","mossUnifiedInternal","mossInferredBody",
                "mossCartridgeCardPart","mossTofModulePart","mossCompactDrivePart","mossJointDrive")
    if not guard and (any(obj.get(k) for k in internal) or any("机身内部 · 整组移动" in o.name for o in names)):
        return None
    if any(o.get("mossPartId") in {"pcb","servo-yaw","servo-pitch"} for o in names):
        return None
    if "wristPitch" in rigs:
        return "head"
    if "elbowYaw" in rigs:
        return "elbow"
    if "shoulderYaw" in rigs:
        return "shoulder"
    return "mount"


def pbr(original, cache, ranges):
    if not original:
        return None
    if original in cache:
        return cache[original]
    if not original.node_tree:
        return original
    source = next((n for n in original.node_tree.nodes if n.type=="BSDF_PRINCIPLED"),None)
    if not source:
        return original
    mat = bpy.data.materials.new("Web "+original.name)
    cache[original] = mat
    mat.use_nodes = True
    mat.use_backface_culling = original.use_backface_culling
    sh = next(n for n in mat.node_tree.nodes if n.type=="BSDF_PRINCIPLED")
    ranges[original.name]=link_pbr(original,mat,source,sh)
    sh.inputs["Emission Strength"].default_value=source.inputs["Emission Strength"].default_value
    for key in ("Emission Color",):
        image,val = first_layer(source.inputs[key])
        if image:
            tex=mat.node_tree.nodes.new("ShaderNodeTexImage")
            tex.image=image
            mat.node_tree.links.new(tex.outputs["Color"],sh.inputs[key])
        elif val is not None and hasattr(val,"__len__") and len(val)==4:
            sh.inputs[key].default_value=val
    return mat


def export(args):
    src=Path(bpy.data.filepath).resolve()
    out=args.output.resolve()
    assert out.suffix.lower()==".glb" and src.is_file()
    out.parent.mkdir(parents=True,exist_ok=True)
    scene=bpy.data.scenes["MOSS · 自然展示"]
    bpy.context.window.scene=scene
    if complete_view := scene.view_layers.get("01 · 完整装配"):
        bpy.context.window.view_layer=complete_view
    scene.frame_set(2676)
    shell,=[o for o in scene.objects if o.get("mossSourceName",o.name).startswith("P06_主体后壳_X5版-1@")]
    inverse=shell.matrix_world.inverted()
    rigs={o["mossRig"]:o for o in scene.objects if o.get("mossRig")}
    anchors={}
    for key, reference in {"shoulderYaw":[43.72372649,199.61925849,-21],"elbowYaw":[109.67351729,170.25529713,-17.32157084],
                           "wristPitch":[83.83040619,63.62100068,-18.83231797],"headAssembly":[43.5,76,-26.5]}.items():
        point=inverse @ rigs[key].matrix_world.translation
        point=Vector((point.x,point.z,-point.y))
        assert (point-Vector(reference)).length < .01,(key,point,reference)
        anchors[key]=list(point)
    web=bpy.data.scenes.new("MOSS release exterior")
    source_hash=digest(src)
    web["sourceFile"]=src.name
    web["sourceSha256"]=source_hash
    roots={}
    for name in ("mount","shoulder","elbow","head"):
        root=bpy.data.objects.new(name,None)
        root["exteriorGroup"]=name
        root["sourceFile"]=src.name
        root["sourceSha256"]=source_hash
        web.collection.objects.link(root)
        roots[name]=root
    graph=bpy.context.evaluated_depsgraph_get()
    cache={}
    pbr_ranges={}
    counts=Counter()
    wear=[]
    details=Counter()
    joint_normals=[]
    recipe=read_recipes(args.recipe)
    for srcobj in scene.objects:
        if srcobj.type!="MESH" or srcobj.hide_render or not srcobj.visible_get():
            continue
        group=exterior_group(srcobj)
        if not group:
            continue
        ev=srcobj.evaluated_get(graph)
        mesh=bpy.data.meshes.new_from_object(ev,preserve_all_data_layers=True,depsgraph=graph)
        label=srcobj.get("mossSourceName",srcobj.name)
        transform=inverse @ ev.matrix_world
        joint_fix=restore_joint_normals(mesh,label,transform,anchors)
        if joint_fix:
            joint_normals.append(joint_fix)
            print("圆筒法线",joint_fix,flush=True)
        for i,slot in enumerate(srcobj.material_slots):
            original=slot.material
            if original:
                mat=pbr(original,cache,pbr_ranges)
                if original.get("mossExteriorUseWear"):
                    stats=make_wear(mesh,original,label,recipe,mat)
                    print("磨损",label,stats,flush=True)
                    wear.append({"sourceName":label,**stats})
                mesh.materials[i]=mat
        obj=bpy.data.objects.new("CAD-"+srcobj.name,mesh)
        web.collection.objects.link(obj)
        obj.parent=roots[group]
        obj.matrix_world=transform
        obj["sourceName"]=label
        obj["sourceId"]=srcobj.get("mossId","")
        for key in ("mossGroup","mossTofRole","mossDecal","mossFinalPropSideHatch","mossSwitchContinuousRoundover","mossUndersidePlanarNormalAudit"):
            value=srcobj.get(key)
            if value:
                obj[key]=value if isinstance(value,(bool,int,float,str)) else True
                details[key]+=1
        counts[group]+=1
    assert len(wear)>=13 and details["mossDecal"]>=18 and details["mossSwitchContinuousRoundover"]>=3
    assert any(o.get("mossTofRole")=="tof-service-gear-guard" for o in web.objects)
    assert all(counts[key]>10 for key in roots)
    bpy.context.window.scene=web
    bpy.context.view_layer.update()
    stage=out.with_name(out.stem+".building.glb")
    try:
        write_exterior_gltf(stage)
        info={"sourceFile":src.name,"sourceSha256":source_hash,"blend":src.name,"sha256":source_hash,
              "glbSha256":digest(stage),"bytes":stage.stat().st_size,"frame":2676,"groups":dict(counts),
              "coordinates":"CAD mm, Y up; parent groups identity in neutral assembly","anchors":anchors,
              "attributes":dict(details),"wear":wear,"sourceScene":scene.name,"compression":"EXT_meshopt_compression",
              "jointNormals":joint_normals,"meshoptExponentialBits":EXPONENTIAL_BITS,
              "materialRanges":pbr_ranges}
        manifest=out.with_suffix(".json")
        temp=manifest.with_suffix(".building.json")
        temp.write_text(json.dumps(info,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        stage.replace(out)
        temp.replace(manifest)
        print(out,info["bytes"],counts,flush=True)
        if args.batch_output:
            from exterior_batch import export_batched
            export_batched(web,args.batch_output.resolve(),info)
    finally:
        stage.unlink(missing_ok=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--recipe",type=Path,required=True)
    parser.add_argument("--batch-output",type=Path)
    export(parser.parse_args(sys.argv[sys.argv.index("--")+1:]))
