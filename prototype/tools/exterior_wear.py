import ast
from pathlib import Path

import bpy
from mathutils.bvhtree import BVHTree
import numpy as np


def read_recipes(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    keys = {"PAINT", "METAL", "STROKES", "CHIPS"}
    recipes = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
               and isinstance(n.targets[0], ast.Name) and n.targets[0].id in keys}
    assert recipes.keys() == keys
    return recipes


def first_layer(socket):
    while socket.is_linked:
        node = socket.links[0].from_node
        if node.type == "TEX_IMAGE":
            return node.image, None
        if node.type == "MIX_RGB":
            socket = node.inputs[1]
        elif node.type == "MIX":
            socket = next(i for i in node.inputs if i.enabled and i.name == "A")
        else:
            break
    return None, socket.default_value


def smooth(low, high, value):
    t = np.clip((value - low) / (high - low), 0, 1)
    return t * t * (3 - 2 * t)


def grain(position, scale):
    p = position * scale
    whole = np.floor(p)
    fraction = smooth(0., 1., p - whole)
    value = np.zeros(len(position))
    for x in (0, 1):
        for y in (0, 1):
            for z in (0, 1):
                corner = np.array([x, y, z])
                phase = (whole + corner) @ [127.1, 311.7, 74.7]
                noise = np.sin(phase) * 43758.5453
                value += np.prod(np.where(corner, fraction, 1 - fraction), axis=1) * (noise - np.floor(noise))
    return value


def focus(p, patches):
    mask = np.zeros(len(p))
    for center, radii in patches:
        distance = np.linalg.norm((p - center) / radii, axis=1)
        mask = np.maximum(mask, 1 - smooth(.14, 1., distance))
    return mask


def image_pixels(image):
    width, height = image.size
    values = np.empty(width * height * 4, np.float32)
    image.pixels.foreach_get(values)
    return values.reshape(height, width, 4)


def mix(rgb, factor, tint):
    return rgb * (1 - factor[:, None]) + np.asarray(tint) * factor[:, None]


def make_wear(mesh, original, source_name, recipes, mat):
    key = next((key for key in (*recipes["PAINT"], *recipes["METAL"]) if source_name.startswith(key)), None)
    if key is None:
        raise ValueError(f"新磨损材质没有对应的源接触区定义：{source_name}")
    painted = key in recipes["PAINT"]
    patches = recipes["PAINT" if painted else "METAL"][key]
    nodes = original.node_tree.nodes
    shader = next(n for n in nodes if n.type == "BSDF_PRINCIPLED")
    combine = next(n for n in nodes if n.label == "相对零件装配面定位，随动")
    basis, origin = [], []
    for inp in combine.inputs:
        add = inp.links[0].from_node
        dot = add.inputs[0].links[0].from_node
        basis.append(list(dot.inputs[1].default_value))
        origin.append(add.inputs[1].default_value)
    mesh.calc_loop_triangles()
    tris = np.array([list(t.vertices) for t in mesh.loop_triangles], dtype=np.int32)
    loops = np.array([list(t.loops) for t in mesh.loop_triangles], dtype=np.int32)
    xyz = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", xyz)
    xyz = xyz.reshape(-1, 3)
    lo, hi = xyz.min(axis=0), xyz.max(axis=0)
    span = np.maximum(hi - lo, .0001)
    tree = BVHTree.FromPolygons(xyz.tolist(), tris.tolist(), all_triangles=True)
    original_uv = mesh.uv_layers.active
    uv_data = None
    if original_uv:
        uv_data = np.empty(len(mesh.loops) * 2, np.float32)
        original_uv.data.foreach_get("uv", uv_data)
        uv_data = uv_data.reshape(-1, 2)
    picture, base = first_layer(shader.inputs["Base Color"])
    pixels = image_pixels(picture) if picture and uv_data is not None else None
    fallback = np.asarray(list(base)[:3] if base is not None else [.65, .63, .59])
    resolution = 192 if source_name.startswith(("P06_", "P08_")) else 96
    atlas = np.ones((2 * resolution, 3 * resolution, 4), dtype=np.float32)
    atlas[:, :, :3] = fallback
    axes = [(1, 2), (0, 2), (0, 1)]
    pad = 2
    covered = 0
    max_change = 0.
    for axis, (u, v) in enumerate(axes):
        for sign in (0, 1):
            hits, face, indices = [], [], []
            ray = [0., 0., 0.]
            ray[axis] = -1. if sign else 1.
            position = list(lo)
            position[axis] = hi[axis] + 1 if sign else lo[axis] - 1
            for j in range(resolution):
                position[v] = lo[v] + np.clip((j + .5 - pad) / (resolution - 2 * pad), .000001, .999999) * span[v]
                for i in range(resolution):
                    position[u] = lo[u] + min(.999999, max(.000001, (i + .5 - pad) / (resolution - 2 * pad))) * span[u]
                    loc, _, index, _ = tree.ray_cast(position, ray)
                    if loc is not None:
                        hits.append(tuple(loc))
                        face.append(index)
                        indices.append((j + resolution * sign, i + resolution * axis))
            if not hits:
                continue
            local = np.asarray(hits)
            p = local @ np.array(basis).T + origin
            if pixels is not None:
                vertices = xyz[tris[face]]
                a, b = vertices[:, 1] - vertices[:, 0], vertices[:, 2] - vertices[:, 0]
                offset = local - vertices[:, 0]
                aa, bb, ab = (np.einsum("ij,ij->i", l, r) for l, r in ((a,a),(b,b),(a,b)))
                da, db = np.sum(offset*a,axis=1), np.sum(offset*b,axis=1)
                den = np.maximum(aa*bb - ab*ab, 1e-25)
                wa, wb = (bb*da-ab*db)/den, (aa*db-ab*da)/den
                uv = np.einsum("ij,ijk->ik", np.stack([1-wa-wb,wa,wb],axis=1), uv_data[loops[face]]) % 1
                rgb = pixels[(uv[:,1]*pixels.shape[0]).astype(int), (uv[:,0]*pixels.shape[1]).astype(int), :3]
                layer = next((n for n in nodes if n.label == "保留原色贴图与材质分区" and n.type=="MIX_RGB"),None)
                if layer and not layer.inputs[0].is_linked:
                    t=layer.inputs[0].default_value
                    rgb=rgb*(1-t)+np.array(layer.inputs[2].default_value[:3])*t
            else:
                rgb = np.broadcast_to(fallback, (len(p), 3))
            base_rgb = rgb.copy()
            env, broad, grit = focus(p, patches), grain(p, .51), grain(p, 6.4)
            matte = env * (.07 + .33 * smooth(.28, .66, broad))
            fine = env * (.25 * smooth(.46,.67,broad)) * (1-smooth(.01,.13, np.abs(grain(p*[2.1,.43,5.1],1.3)-.5)))
            if painted:
                rgb = mix(rgb, matte, (.41,.421,.410))
                rgb = mix(rgb, np.clip(.48*fine + env*.68*smooth(.625,.755,grit),0,1), (.385,.402,.408))
                scratches = np.zeros(len(p))
                for start,end,widths in recipes["STROKES"].get(key, ()):
                    rel = (p-start) / widths
                    run=(np.asarray(end)-start)/widths
                    along=np.clip(rel@run / np.dot(run,run),0,1)
                    distance=np.linalg.norm(rel-along[:,None]*run,axis=1)
                    scratches=np.maximum(scratches,1-smooth(.15,1.,distance))
                rgb=mix(rgb,scratches*(.38+.49*smooth(.10,.71,grit)),(.251,.268,.276))
                chips=focus(p,recipes["CHIPS"].get(key,())) * .88*smooth(.45,.68,grain(p,3.2))
                rgb=mix(rgb,chips,(.162,.180,.185))
            else:
                rgb=mix(rgb,fine*.32,(.44,.426,.379))
            change = np.max(np.abs(rgb-base_rgb),axis=1)
            covered += int(np.count_nonzero(change > .0001))
            max_change = max(max_change,float(change.max()))
            rows,cols = np.asarray(indices).T
            atlas[rows,cols,:3] = rgb
    uv_layer = mesh.uv_layers.new(name="ReleaseWear")
    axis_lookup = np.asarray([np.argmax(np.abs(f.normal)) for f in mesh.polygons])
    new_uv = np.empty((len(mesh.loops), 2), np.float32)
    for poly, axis in zip(mesh.polygons, axis_lookup):
        u,v = axes[axis]
        idx = np.asarray(poly.vertices)
        coords=(xyz[idx]-lo)/span
        steps=resolution-2*pad
        new_uv[poly.loop_start:poly.loop_start+poly.loop_total,0]=(axis+(pad+steps*coords[:,u])/resolution)/3
        new_uv[poly.loop_start:poly.loop_start+poly.loop_total,1]=(int(poly.normal[axis]>0)+(pad+steps*coords[:,v])/resolution)/2
    uv_layer.data.foreach_set("uv",new_uv.ravel())
    if original_uv:
        mesh.uv_layers.active=original_uv
    image=bpy.data.images.new("接触磨损 "+source_name,width=3*resolution,height=2*resolution,alpha=False)
    # Blender 将线性色转换成打包 PNG 的 sRGB。
    image.pixels.foreach_set(atlas.ravel())
    image.pack()
    uvnode=mat.node_tree.nodes.new("ShaderNodeUVMap")
    uvnode.uv_map=uv_layer.name
    tex=mat.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image=image
    mat.node_tree.links.new(uvnode.outputs["UV"],tex.inputs["Vector"])
    out=next(n for n in mat.node_tree.nodes if n.type=="BSDF_PRINCIPLED")
    mat.node_tree.links.new(tex.outputs["Color"],out.inputs["Base Color"])
    if not covered:
        raise ValueError("接触区没有投到导出表面：" + source_name)
    return {"material": original.name, "contactAreas":len(patches), "changedTexels":covered,
            "maxLinearColorChange":round(max_change,6),"dimensions":[3*resolution,2*resolution]}
