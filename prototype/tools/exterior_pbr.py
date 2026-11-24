import bpy
import numpy as np

from exterior_wear import image_pixels


class Samples:
    """在原 UV 上读取材质图像。低于网页纹理分辨率的程序噪声使用中值。"""

    def __init__(self, size, first_row=0, last_row=None, images=None):
        last_row=size if last_row is None else last_row
        y,x=np.mgrid[first_row:last_row,0:size]
        self.uv=np.stack([(x.ravel()+.5)/size,(y.ravel()+.5)/size,np.zeros(x.size)],axis=1)
        self.nodes={}
        self.images={} if images is None else images

    def value(self, val):
        arr=np.asarray(val,dtype=np.float32).reshape(1,-1)
        return np.broadcast_to(arr, (len(self.uv),arr.shape[1]))

    def read(self, socket):
        if not socket.is_linked:
            val=socket.default_value
            return self.value(list(val)[:3] if hasattr(val,"__len__") else val)
        link=socket.links[0]
        node, output=link.from_node,link.from_socket.name
        key=(node.as_pointer(),output)
        if key not in self.nodes:
            self.nodes[key]=self.eval(node,output)
        return self.nodes[key]

    def eval(self,n,output):
        kind=n.type
        inp=lambda i:self.read(n.inputs[i])
        scalar=lambda x:x[:,:1]
        if n.label in {"带位置的浅擦痕","薄擦拭印","局部露底","接触区抛光","合并局部露底","并合接触区",
                       "向周边渐隐","擦痕汇总","包含细点缺漆","相邻细擦片","实际座圈边缘擦亮"}:
            return self.value(0)
        if kind=="TEX_IMAGE":
            image=n.image
            if not image:
                return self.value(1)
            if image.name not in self.images:
                self.images[image.name]=image_pixels(image)
            px=self.images[image.name]
            uv=inp("Vector")[:,:2] if n.inputs["Vector"].is_linked else self.uv[:,:2]
            uv=np.clip(uv,0,.9999999) if n.extension in ("CLIP","EXTEND") else uv % 1.
            col=px[(uv[:,1]*px.shape[0]).astype(int),(uv[:,0]*px.shape[1]).astype(int)]
            return col[:,3:4] if output=="Alpha" else col[:,:3]
        if kind=="TEX_COORD":
            return self.uv if output=="UV" else self.value((0.,0.,0.))
        if kind=="UVMAP":
            return self.uv
        if kind=="MAPPING":
            angles=n.inputs["Rotation"].default_value
            result=inp("Vector")*inp("Scale")
            if any(abs(a)>1e-7 for a in angles):
                from mathutils import Euler
                result=result@np.asarray(Euler(tuple(angles)).to_matrix()).T
            return result+inp("Location")
        if kind=="SEPARATE_COLOR":
            i=("Red","Green","Blue").index(output)
            return inp(0)[:,i:i+1]
        if kind=="SEPXYZ":
            i="XYZ".index(output)
            return inp(0)[:,i:i+1]
        if kind=="COMBXYZ":
            return np.hstack([scalar(inp(i)) for i in range(3)])
        if kind in ("TEX_NOISE","TEX_VORONOI"):
            return self.value(.5)
        if kind=="VALUE":
            return self.value(n.outputs[0].default_value)
        if kind in ("MIX_RGB","MIX"):
            if kind=="MIX_RGB":
                factor,a,b=inp(0),inp(1),inp(2)
            else:
                factor,a,b=[self.read(i) for i in n.inputs if i.enabled and i.name in ("Factor","A","B")]
            t=np.clip(factor,0,1)
            return (1-t)*a+t*(a*b if n.blend_type=="MULTIPLY" else b)
        if kind=="MAP_RANGE":
            lo,hi=inp("From Min"),inp("From Max")
            t=(inp("Value")-lo)/np.where(hi!=lo,hi-lo,1)
            if n.clamp or n.interpolation_type!="LINEAR":
                t=np.clip(t,0,1)
            if n.interpolation_type=="SMOOTHSTEP":
                t=t*t*(3-2*t)
            return inp("To Min")+t*(inp("To Max")-inp("To Min"))
        if kind=="VALTORGB":
            ramp=n.color_ramp.elements
            return np.stack([np.interp(inp(0)[:,0],[c.position for c in ramp],[c.color[i] for c in ramp]) for i in range(3)],axis=1)
        if kind in ("VECT_MATH","MATH"):
            a=inp(0)
            op=n.operation
            if op=="LENGTH": return np.linalg.norm(a,axis=1,keepdims=True)
            if op=="SINE": return np.sin(a)
            if op=="FRACT": return a-np.floor(a)
            if op=="ABSOLUTE":return np.abs(a)
            b=inp(1)
            if op=="MULTIPLY_ADD":return a*b+inp(2)
            if op=="DOT_PRODUCT":return np.sum(a*b,axis=1,keepdims=True)
            return {"MULTIPLY":np.multiply,"ADD":np.add,"SUBTRACT":np.subtract,"MINIMUM":np.minimum,
                    "MAXIMUM":np.maximum,"LESS_THAN":np.less}[op](a,b)
        raise ValueError(f"网页 PBR 尚未处理材质节点 {n.name} / {kind} / {output}")


def image_node(mat,image):
    node=mat.node_tree.nodes.new("ShaderNodeTexImage")
    node.image=image
    return node


def generated_image(name,size,values,*,data):
    image=bpy.data.images.new(name,width=size,height=size,alpha=False,is_data=data)
    if data: image.colorspace_settings.name="Non-Color"
    rgba=np.ones((size*size,4),dtype=np.float32)
    rgba[:,:3]=np.clip(values,0,1)
    image.pixels.foreach_set(rgba.ravel())
    image.pack()
    return image


def link_pbr(original, mat, src, dest):
    images=[n.image for n in original.node_tree.nodes if n.type=="TEX_IMAGE" and n.image]
    count=192 if images else 1
    samples=Samples(count)
    link=mat.node_tree.links.new
    channels={}
    for name in ("Roughness","Metallic","Coat Weight","Coat Roughness","Transmission Weight","Specular IOR Level"):
        values=np.clip(samples.read(src.inputs[name])[:,:1],0,1)
        dest.inputs[name].default_value=float(np.median(values))
        channels[name]=values
    rough=channels["Roughness"]
    metal=channels["Metallic"]
    channel_size=count
    if np.ptp(rough)>.01 or np.ptp(metal)>.01:
        # 原 CAD 的漆面与金属交界有窄 UV 岛。金属度变化大的贴图保留源采样尺度。
        if images and np.ptp(metal)>.25:
            channel_size=max(count,min(1024,max(max(image.size) for image in images)))
            if channel_size>count:
                high_rough=np.empty((channel_size*channel_size,1),dtype=np.float32)
                high_metal=np.empty_like(high_rough)
                for first in range(0,channel_size,128):
                    last=min(channel_size,first+128)
                    native=Samples(channel_size,first,last,samples.images)
                    rows=slice(first*channel_size,last*channel_size)
                    high_rough[rows]=np.clip(native.read(src.inputs["Roughness"])[:,:1],0,1)
                    high_metal[rows]=np.clip(native.read(src.inputs["Metallic"])[:,:1],0,1)
                rough,metal=high_rough,high_metal
        pixels=np.hstack([np.ones_like(rough),rough,metal])
        image=generated_image("材质通道 "+original.name,channel_size,pixels,data=True)
        tex=image_node(mat,image)
        separate=mat.node_tree.nodes.new("ShaderNodeSeparateColor")
        link(tex.outputs["Color"],separate.inputs[0])
        link(separate.outputs["Green"],dest.inputs["Roughness"])
        link(separate.outputs["Blue"],dest.inputs["Metallic"])
    base=src.inputs["Base Color"]
    alpha=src.inputs["Alpha"]
    base_tex=base.links[0].from_node if base.is_linked and base.links[0].from_node.type=="TEX_IMAGE" else None
    if base_tex:
        tex=image_node(mat,base_tex.image)
        link(tex.outputs["Color"],dest.inputs["Base Color"])
    elif not original.get("mossExteriorUseWear"):
        color=samples.read(base)
        if len(color)>1 and np.max(np.ptp(color,axis=0))>.012:
            tex=image_node(mat,generated_image("原色 "+original.name,count,color,data=False))
            link(tex.outputs["Color"],dest.inputs["Base Color"])
        else:
            dest.inputs["Base Color"].default_value=(*np.median(color,axis=0)[:3],1)
    if alpha.is_linked and alpha.links[0].from_node.type=="TEX_IMAGE":
        srcnode=alpha.links[0].from_node
        if base_tex!=srcnode:
            tex=image_node(mat,srcnode.image)
        link(tex.outputs["Alpha"],dest.inputs["Alpha"])
        mat.surface_render_method=original.surface_render_method
    else:
        dest.inputs["Alpha"].default_value=float(np.median(samples.read(alpha)))
    return {"roughness":[round(float(v),4) for v in (rough.min(),np.median(rough),rough.max())],
            "metallic":[round(float(v),4) for v in (metal.min(),np.median(metal),metal.max())],
            "channelSize":channel_size}
