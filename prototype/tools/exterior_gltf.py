import bpy
from io_scene_gltf2.io.exp import meshopt


EXPONENTIAL_BITS = 20


def write_exterior_gltf(path):
    # 整机以 CAD 毫米导出。默认 12 位共享指数会让圆筒侧壁上的窄三角面塌缩。
    original_bits = meshopt.EXP_FILTER_BITS
    meshopt.EXP_FILTER_BITS = EXPONENTIAL_BITS
    try:
        bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_active_scene=True, export_yup=True,
                                  export_animations=False, export_cameras=False, export_lights=False, export_extras=True,
                                  export_skins=False, export_morph=False, export_meshopt_compression_enable=True,
                                  export_meshopt_extension="EXT_meshopt_compression")
    finally:
        meshopt.EXP_FILTER_BITS = original_bits
